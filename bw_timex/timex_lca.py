import warnings
from datetime import datetime
from itertools import chain
from typing import Callable, Optional

import bw2data as bd
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sb
from bw2calc import LCA
from bw2data import (
    Database,
    Method,
    Normalization,
    Weighting,
    databases,
    methods,
    normalizations,
    projects,
    weightings,
)
from bw2data.backends.schema import ActivityDataset as AD
from bw2data.backends.schema import get_id
from bw2data.errors import Brightway2Project
from dynamic_characterization import characterize_dynamic_inventory
from peewee import fn
from scipy import sparse

# from .dynamic_biosphere_builder import DynamicBiosphereBuilder
from .dynamic_biosphere_builder import DynamicBiosphereBuilder
from .helper_classes import SetList, TimeMappingDict
from .matrix_modifier import MatrixModifier
from .timeline_builder import TimelineBuilder
from .utils import extract_date_as_integer


class TimexLCA:
    """
    Class to perform time-explicit LCA calculations.

    A TimexLCA retrieves the LCI of processes occuring at explicit points in time and relinks their technosphere
    exchanges to match the technology landscape at that point in time, while keeping track of the timing of the
    resulting emissions. As such, it combines prospective and dynamic LCA approaches.

    TimexLCA first calculates a static LCA, which informs a priority-first graph traversal. From the graph traversal,
    temporal relationships between exchanges and processes are derived. Based on the timing of the processes, bw_timex
    matches the processes at the intersection between foreground and background to the best available background
    databases. This temporal relinking is achieved by using datapackages to add new time-specific processes. The new
    processes and their exchanges to other technosphere processes or biosphere flows extent the technopshere and
    biosphere matrices.

    Temporal information of both processes and biosphere flows are retained, allowing for dynamic LCIA.

    Currently absolute Temporal Distributions for biosphere exchanges are dealt with as a look up function:
    If an activity happens at timestamp X then and the biosphere exchange has an absolute temporal 
    distribution (ATD), it looks up the amount from from the ATD correspnding to timestamp X. 
    E.g.: X = 2024, TD=(data=[2020,2021,2022,2023,2024,.....,2120 ], amount=[3,4,4,5,6,......,3]),
    it will look up the value 6 corresponding 2024. If timestamp X does not exist it find the nearest 
    timestamp available (if two timestamps are equally close, it will take the first in order of
    apearance (see numpy.argmin() for this behabiour). 


    TimexLCA calculates:
     1) a static LCA score (`TimexLCA.static_lca.score`, same as `bw2calc.lca.score`),
     2) a static time-explicit LCA score (`TimexLCA.static_score`), which links LCIs to the respective background databases but without additional temporal dynamics of the biosphere flows,
     3) a dynamic time-explicit LCA score (`TimexLCA.dynamic_score`), with dynamic inventory and dynamic charaterization factors. These are provided for radiative forcing and GWP but can also be user-defined.

    Example
    -------
    >>> demand = {('my_foreground_database', 'my_process'): 1}
    >>> method = ("some_method_family", "some_category", "some_method")
    >>> database_date_dict = {
            'my_background_database_one': datetime.strptime("2020", "%Y"),
            'my_background_database_two': datetime.strptime("2030", "%Y"),
            'my_foreground_database':'dynamic'
        }
    >>> bw_timex = TimexLCA(demand, method, database_date_dict)
    >>> bw_timex.build_timeline() # you can pass many optional arguments here, also for the graph traversal
    >>> bw_timex.lci()
    >>> bw_timex.static_lcia()
    >>> print(bw_timex.static_score)
    >>> bw_timex.dynamic_lcia(metric="radiative_forcing") # different metrics can be used, e.g. "GWP", "radiative_forcing"
    >>> print(bw_timex.dynamic_score)

    """ """"""

    def __init__(
        self,
        demand: dict,
        method: tuple,
        database_date_dict: dict = None,
    ) -> None:
        """
        Instantiating a `TimexLCA` object calculates a static LCA, initializes time mapping dicts for activities and biosphere flows, and stores useful subsets of ids in the node_id_collection_dict.

        Parameters
        ----------
        demand : dict[object: float]
                The demand for which the LCA will be calculated. The keys can be Brightway `Node`
                instances, `(database, code)` tuples, or integer ids.
        method : tuple
                Tuple defining the LCIA method, such as `('foo', 'bar')` or default methods, such as `("EF v3.1", "climate change", "global warming potential (GWP100)")`
        database_date_dict : dict, optional
                Dictionary mapping database names to dates.
        """

        self.demand = demand
        self.method = method
        self.database_date_dict = database_date_dict

        if not self.database_date_dict:
            warnings.warn(
                "No database_date_dict provided. Treating the databases containing the functional unit as dynamic. No remapping to time explicit databases will be done."
            )
            self.database_date_dict = {key[0]: "dynamic" for key in demand.keys()}

        # Create static_only dict that excludes dynamic processes that will be exploded later. This way we only have the "background databases" that we can later link to from the dates of the timeline.
        self.database_date_dict_static_only = {
            k: v for k, v in self.database_date_dict.items() if isinstance(v, datetime)
        }

        # Create some collections of nodes that will be useful down the line, e.g. all nodes from the background databases that link to foregroud nodes.
        self.create_node_id_collection_dict()

        # Calculate static LCA results using a custom prepare_lca_inputs function that includes all background databases in the LCA. We need all the IDs for the time mapping dict.
        fu, data_objs, remapping = self.prepare_static_lca_inputs(
            demand=self.demand, method=self.method
        )
        self.static_lca = LCA(fu, data_objs=data_objs, remapping_dicts=remapping)
        self.static_lca.lci()
        self.static_lca.lcia()

        # Create a time mapping dict that maps each activity to a activity_time_mapping_id in the format (('database', 'code'), datetime_as_integer): time_mapping_id)
        self.activity_time_mapping_dict = TimeMappingDict(
            start_id=bd.backends.ActivityDataset.select(fn.MAX(AD.id)).scalar() + 1
        )  # making sure to use unique ids for the time mapped processes by counting up from the highest current activity id

        # Create a similar dict for the biosphere flows. This is populated by the dynamic_biosphere_builder
        self.biosphere_time_mapping_dict = TimeMappingDict(start_id=0)

    ########################################
    # Main functions to be called by users #
    ########################################

    def build_timeline(
        self,
        temporal_grouping: str = "year",
        interpolation_type: str = "linear",
        edge_filter_function: Callable = None,
        cutoff: float = 1e-9,
        max_calc: float = 1e4,
        *args,
        **kwargs,
    ) -> pd.DataFrame:
        """
        Creates a `TimelineBuilder` instance that does the graph traversal (similar to bw_temporalis) and extracts all
        edges with their temporal information. Creates the `TimexLCA.timeline` of exchanges.

        Parameters
        ----------
        temporal_grouping : str, optional
            Time resolution for grouping exchanges over time in the timeline. Default is 'year', other options are
            'month', 'day', 'hour'.
        interpolation_type : str, optional
            Type of interpolation when sourcing the new producers in the time-explicit background databases. Default is
            'linear', which means linear interpolation between the closest 2 databases, other options are 'closest',
            which selects only the closest database.
        edge_filter_function : Callable, optional
            Function to skip edges in the graph traversal. Default is to skip all edges within background databases.
        cutoff: float, optional
            The cutoff value for the graph traversal. Default is 1e-9.
        max_calc: float, optional
            The maximum number of calculations to be performed by the graph traversal. Default is 1e4.
        *args : iterable
            Positional arguments for the graph traversal. for `bw_temporalis.TemporalisLCA` passed to the
            `EdgeExtractor` class, which inherits from `TemporalisLCA`.
            See `bw_temporalis` documentation for more information.
        **kwargs : dict
            Additional keyword arguments for `bw_temporalis.TemporalisLCA` passed to the EdgeExtractor class, which
            inherits from TemporalisLCA.
            See bw_temporalis documentation for more information.

        Returns
        -------
        pandas.DataFrame:
            A DataFrame containing the timeline of exchanges

        See also
        --------
        bw_timex.timeline_builder.TimelineBuilder: Class that builds the timeline.

        """
        if edge_filter_function is None:
            warnings.warn(
                "No edge filter function provided. Skipping all edges within background databases."
            )
            skippable = []
            for db in self.database_date_dict_static_only.keys():
                skippable.extend([node.id for node in bd.Database(db)])
            self.edge_filter_function = lambda x: x in skippable
        else:
            self.edge_filter_function = edge_filter_function

        self.temporal_grouping = temporal_grouping
        self.interpolation_type = interpolation_type
        self.cutoff = cutoff
        self.max_calc = max_calc

        # pre-populate the activity time mapping dict with the static activities.
        # Doing this here because we need the temporal grouping for consistent times resolution.
        self.add_static_activities_to_time_mapping_dict()

        # Create timeline builder that does the graph traversal (similar to bw_temporalis) and extracts
        # all edges with their temporal information. Can later be used to build a timeline with the
        # TimelineBuilder.build_timeline() method.
        self.timeline_builder = TimelineBuilder(
            self.static_lca,
            self.edge_filter_function,
            self.database_date_dict,
            self.database_date_dict_static_only,
            self.activity_time_mapping_dict,
            self.node_id_collection_dict,
            self.temporal_grouping,
            self.interpolation_type,
            self.cutoff,
            self.max_calc,
            *args,
            **kwargs,
        )

        self.timeline = self.timeline_builder.build_timeline()
        return self.timeline[
            [
                "date_producer",
                "producer_name",
                "date_consumer",
                "consumer_name",
                "amount",
                "interpolation_weights",
            ]
        ]

    def lci(
        self,
        build_dynamic_biosphere: Optional[bool] = True,
        expand_technosphere: Optional[bool] = True,
    ) -> None:
        """
        Calculates the time-explicit LCI.

        Generates the biosphere and technosphere modifications via the `MatrixModifier` class by calling `TimexLCA.
        build_datapackage() if `expand_technosphere' is True. Otherwise it generates a dynamic inventory directly from
        from the timeline. Optionally, the dynamic biosphere matrix and dynamic inventory is calculated via
        `TimexLCA.calculate_dynamic_inventory()`. Set `build_dynamic_biosphere` to False if you only want to get a new
        overall score and don't care about the timing
        of the emissions. This saves time and memory.

        Parameters
        ----------
        build_dynamic_biosphere: bool
            if True, build the dynamic biosphere matrix and calculate the dynamic LCI.
            Default is True.
        expand_technosphere: bool
            if True, creates an expanded time-explicit technosphere and calculates the LCI from it. If False, creates no
            new technosphere, but calculates the dynamic inventory directly from the timeline. Building from the timeline
            currently only works if `build_dynamic_biosphere` is also True.

        Returns
        -------
        None, but calls LCI calculations from bw2calc and calculates the dynamic inventory, if `build_dynamic_biosphere` is True.

        See also
        --------
        build_datapackage: Method to create the datapackages that contain the modifications to the technopshere and biosphere matrix using the `MatrixModifier` class.
        calculate_dynamic_inventory: Method to calculate the dynamic inventory if `build_dynamic_biosphere` is True.
        """

        if not expand_technosphere and not build_dynamic_biosphere:
            raise ValueError(
                "Currently it is only possible to calculate a dynamic inventory from the timeline.\
                    Please either set build_dynamic_biosphere=True or expand_technosphere=True"
            )

        if not hasattr(self, "timeline"):
            raise ValueError(
                "Timeline not yet built. Call TimexLCA.build_timeline() first."
            )

        # mapping of the demand id to demand time
        self.demand_timing_dict = self.create_demand_timing_dict()

        self.fu, self.data_objs, self.remapping = self.prepare_bw_timex_inputs(
            demand=self.demand,
            method=self.method,
        )

        if expand_technosphere:
            self.datapackage = self.build_datapackage()
            data_obs = self.data_objs + self.datapackage
            self.expanded_technosphere = True  # set flag for later static lcia useage
        else:  # setup for timeline approach
            self.collect_temporalized_processes_from_timeline()
            data_obs = self.data_objs
            self.expanded_technosphere = False  # set flag for later lcia usage

        self.lca = LCA(
            self.fu,
            data_objs=data_obs,
            remapping_dicts=self.remapping,
        )

        if not build_dynamic_biosphere:
            self.lca.lci()
        else:
            if expand_technosphere:
                self.lca.lci(factorize=True)
                self.calculate_dynamic_inventory(from_timeline=False)
                self.lca.redo_lci(
                    self.fu
                )  # to get back the original LCI - necessary because we do some redo_lci's in the dynamic inventory calculation
            else:
                self.calculate_dynamic_inventory(from_timeline=True)

    def static_lcia(self) -> None:
        """
        Calculates static LCIA using time-explicit LCIs with the standard static characterization factors of the selected LCIA method using `bw2calc.lcia()`.

        Parameters
        ----------
        None

        Returns
        -------
        None, but stores the static score in the attribute `static_score`.
        """
        if not hasattr(self, "lca"):
            raise AttributeError("LCI not yet calculated. Call TimexLCA.lci() first.")
            return
        if not self.expanded_technosphere:
            raise ValueError("Currently the static lcia score can only be calculated if the expanded matrix has been built\
                             Please call TimexLCA.lci(expand_technosphere=True) first.")
        self.lca.lcia()
        self.static_score = self.lca.score

    def dynamic_lcia(
        self,
        metric: str = "radiative_forcing",
        time_horizon: int = 100,
        fixed_time_horizon: bool = False,
        time_horizon_start: datetime = None,
        characterization_function_dict: dict = None,
        characterization_function_co2: dict = None,
    ) -> pd.DataFrame:
        """
        Calculates dynamic LCIA with the `DynamicCharacterization` class using the dynamic inventory and dynamic
        characterization functions. Dynamic characterization is handled by the separate package 
        `dynamic_characterization` (https://dynamic-characterization.readthedocs.io/en/latest/).

        Dynamic characterization functions in the form of a dictionary {biosphere_flow_database_id:
        characterization_function} can be given by the user.
        If none are given, a set of default dynamic characterization functions based on IPCC AR6 are provided from
        `dynamic_characterization` package. These are mapped to the biosphere3 flows of the chosen static climate 
        change impact category. If there is no characterization function for a biosphere flow, it will be ignored.

        Two dynamic climate change metrics are provided: "GWP" and "radiative_forcing".
        The time horizon for the impact assessment can be set with the `time_horizon` parameter, defaulting to 100 years.
        The `fixed_time_horizon` parameter determines whether the emission time horizon for all emissions is calculated from the
        functional unit (`fixed_time_horizon=True`) or from the time of the emission (`fixed_time_horizon=False`).
        The former is the implementation of the Levasseur approach (see https://doi.org/10.1021/es9030003), while the latter is how conventional LCA is done.

        Parameters
        ----------
        metric : str, optional
            the metric for which the dynamic LCIA should be calculated. Default is "radiative_forcing". Available: "GWP" and "radiative_forcing"
        time_horizon: int, optional
            the time horizon for the impact assessment. Unit is years. Default is 100.
        fixed_time_horizon: bool, optional
            Whether the emission time horizon for all emissions is calculated from the functional unit (fixed_time_horizon=True) or from the time of the emission (fixed_time_horizon=False). Default is False.
        time_horizon_start: pd.Timestamp, optional
            The starting timestamp of the time horizon for the dynamic characterization. Only needed for fixed time horizons. Default is datetime.now().
        characterization_function_dict: dict, optional
            Dict of the form {biosphere_flow_database_id: characterization_function}. Default is None, which triggers the use of the provided dynamic characterization functions based on IPCC AR6 Chapter 7.
        characterization_function_co2: Callable, optional
            Characterization function for CO2 emissions. Necessary if GWP metrix is chosen. Default is None, which triggers the use of the provided dynamic characterization function of co2 based on IPCC AR6 Chapter 7.


        Returns
        -------
        pandas.DataFrame
            A Dataframe with the characterized inventory for the chosen metric and parameters. Also stores the sum as attribute `dynamic_score`.

        See also
        --------
        dynamic_charaterization: Package handling the dynamic characterization: https://dynamic-characterization.readthedocs.io/en/latest/
        """

        if not hasattr(self, "dynamic_inventory"):
            raise AttributeError(
                "Dynamic lci not yet calculated. Call TimexLCA.calculate_dynamic_lci() first."
            )

        # Set a default for inventory_in_time_horizon using the full dynamic_inventory_df
        inventory_in_time_horizon = self.dynamic_inventory_df

        # Calculate the latest considered impact date
        t0_date = pd.Timestamp(self.timeline_builder.edge_extractor.t0.date[0])
        latest_considered_impact = t0_date + pd.DateOffset(years=time_horizon)

        # Update inventory_in_time_horizon if a fixed time horizon is used
        if fixed_time_horizon:
            last_emission = self.dynamic_inventory_df.date.max()
            if latest_considered_impact < last_emission:
                warnings.warn(
                    "An emission occurs outside of the specified time horizon and will not be characterized. Please make sure this is intended."
                )
                inventory_in_time_horizon = self.dynamic_inventory_df[
                    self.dynamic_inventory_df.date <= latest_considered_impact
                ]

        if not time_horizon_start:
            time_horizon_start = t0_date

        self.characterized_inventory = characterize_dynamic_inventory(
            dynamic_inventory_df=inventory_in_time_horizon,
            metric=metric,
            characterization_function_dict=characterization_function_dict,
            base_lcia_method=self.method,
            time_horizon=time_horizon,
            fixed_time_horizon=fixed_time_horizon,
            time_horizon_start=time_horizon_start,
            characterization_function_co2=characterization_function_co2,
        )

        self.dynamic_score = self.characterized_inventory["amount"].sum()

        return self.characterized_inventory

    ###############################################
    # Other core functions for the inner workings #
    ###############################################

    def build_datapackage(self) -> list:
        """
        Creates the datapackages that contain the modifications to the technopshere and biosphere matrix using the `MatrixModifier` class.

        Parameters
        ----------
        None

        Returns
        -------
        list
            List of datapackages that contain the modifications to the technopshere and biosphere matrix

        See also
        --------
        bw_timex.matrix_modifier.MatrixModifier: Class that handles the technosphere and biosphere matrix modifications.
        """
        # Create matrix modifier that creates the new datapackages with the exploded processes and new links to background databases.
        self.matrix_modifier = MatrixModifier(
            self.timeline, self.database_date_dict_static_only, self.demand_timing_dict
        )
        self.node_id_collection_dict["temporal_markets"] = (
            self.matrix_modifier.temporal_market_ids
        )
        self.node_id_collection_dict["temporalized_processes"] = (
            self.matrix_modifier.temporalized_process_ids
        )
        return self.matrix_modifier.create_datapackage()

    def calculate_dynamic_inventory(
        self,
        from_timeline: bool = False,
    ) -> None:
        """
        Calculates the dynamic inventory, by first creating a dynamic biosphere matrix using the `DynamicBiosphereBuilder`
        class and then multiplying it with the dynamic supply array. The dynamic inventory matrix is stored in the attribute `dynamic_inventory`.
        It is also converted to a DataFrame and stored in the attribute `dynamic_inventory_df`.

        Parameters
        ----------
        None

        Returns
        -------
        None, but calculates the dynamic inventory and stores it in the attribute `dynamic_inventory` as a matrix and in `dynamic_inventory_df` as a DataFrame.

        See also
        --------
        bw_timex.dynamic_biosphere_builder.DynamicBiosphereBuilder: Class for creating the dynamic biosphere matrix and inventory.
        """

        if not hasattr(self, "lca"):
            warnings.warn(
                "Static Timex LCA has not been run. Call TimexLCA.lci() first."
            )
            return

        self.len_technosphere_dbs = sum(
            [len(bd.Database(db)) for db in self.database_date_dict.keys()]
        )

        self.dynamic_biosphere_builder = DynamicBiosphereBuilder(
            self.lca,
            self.activity_time_mapping_dict,
            self.biosphere_time_mapping_dict,
            self.demand_timing_dict,
            self.node_id_collection_dict,
            self.temporal_grouping,
            self.database_date_dict,
            self.database_date_dict_static_only,
            self.timeline,
            self.interdatabase_activity_mapping,
            from_timeline=from_timeline,
        )
        self.dynamic_biomatrix = (
            self.dynamic_biosphere_builder.build_dynamic_biosphere_matrix(
                from_timeline=from_timeline
            )
        )

        # Build the dynamic inventory
        count = len(self.dynamic_supply_array)
        diagonal_supply_array = sparse.spdiags(
            [self.dynamic_supply_array], [0], count, count
        )  # diagnolization of supply array keeps the dimension of the process, which we want to pass as additional information to the dynamic inventory dict
        self.dynamic_inventory = self.dynamic_biomatrix @ diagonal_supply_array

        self.biosphere_time_mapping_dict_reversed = {
            v: k for k, v in self.biosphere_time_mapping_dict.items()
        }

        self.activity_time_mapping_dict_reversed = {
            v: k for k, v in self.activity_time_mapping_dict.items()
        }
        self.dynamic_inventory_df = self.create_dynamic_inventory_dataframe(from_timeline)

    def create_dynamic_inventory_dataframe(self, from_timeline=False) -> pd.DataFrame:
        """Brings the dynamic inventory from its matrix form in `dynamic_inventory` into the the format
        of a pandas.DataFrame, with the right structure to later apply dynamic characterization functions.

        Format needs to be:

        +------------+--------+------+----------+
        |   date     | amount | flow | activity |
        +============+========+======+==========+
        |  datetime  |   33   |  1   |    2     |
        +------------+--------+------+----------+
        |  datetime  |   32   |  1   |    2     |
        +------------+--------+------+----------+
        |  datetime  |   31   |  1   |    2     |
        +------------+--------+------+----------+

        - date: datetime, e.g. '2024-01-01 00:00:00'
        - flow: flow id
        - activity: activity id

        Parameters
        ----------
        None

        Returns
        -------
        pandas.DataFrame, dynamic inventory in DataFrame format

        """

        dataframe_rows = []
        for i in range(self.dynamic_inventory.shape[0]):
            row_start = self.dynamic_inventory.indptr[i]
            row_end = self.dynamic_inventory.indptr[i + 1]
            for j in range(row_start, row_end):
                row = i
                col = self.dynamic_inventory.indices[j]
                value = self.dynamic_inventory.data[j]
                
                if from_timeline:
                    emitting_process_id = self.timeline.iloc[col]['time_mapped_producer']
                else:
                    emitting_process_id = self.activity_dict.reversed[col]

                bioflow_id, date = self.biosphere_time_mapping_dict_reversed[
                    row
                ]  # indices are already the same as in the matrix, as we create an entirely new biosphere instead of adding new entries (like we do with the technosphere matrix)

                dataframe_rows.append(
                    (
                        date,
                        value,
                        bioflow_id,
                        emitting_process_id,
                    )
                )

        df = pd.DataFrame(
            dataframe_rows, columns=["date", "amount", "flow", "activity"]
        )

        return df.sort_values(by=["date", "amount"], ascending=[True, False])

    #############
    # For setup #
    #############

    def prepare_static_lca_inputs(
        self,
        demand=None,
        method=None,
        weighting=None,
        normalization=None,
        demands=None,
        remapping=True,
        demand_database_last=True,
    ) -> tuple:
        """
        Prepare LCA input arguments in Brightway2.5 style.

        Adapted bw2data.compat.py

        The difference to the original method is that we load all available databases into the matrices instead of just the ones depending on the demand.
        We need this for the creation of the time mapping dict that creates a mapping between the producer id and the reference timing of the databases in the `database_date_dict`.

        Parameters
        ----------
        demand : dict[object: float]
            The demand for which the LCA will be calculated. The keys can be Brightway `Node`
            instances, `(database, code)` tuples, or integer ids.
        method : tuple
            Tuple defining the LCIA method, such as `('foo', 'bar')`. Only needed if not passing
            `data_objs`.
        weighting : tuple
            Tuple defining the LCIA weighting, such as `('foo', 'bar')`. Only needed if not passing
            `data_objs`.
        normalization: str
        demands: list of dicts of demands
        remapping: bool
            If True, remap dictionaries
        demand_database_last: bool
            If True, add the demand databases last in the list `database_names`.

        Returns
        -------
        tuple
            Indexed demand, data objects, and remapping dictionaries

        See also
        --------
        bw2data.compat.prepare_lca_inputs: Original code this function is adapted from (https://github.com/brightway-lca/brightway2-data/blob/main/bw2data/compat.py).

        """
        if not projects.dataset.data.get("25"):
            raise Brightway2Project(
                "Please use `projects.migrate_project_25` before calculating using Brightway 2.5"
            )

        databases.clean()
        data_objs = []
        remapping_dicts = None

        demand_database_names = list(self.database_date_dict.keys())

        if demand_database_names:
            database_names = set.union(
                *[
                    Database(db_label).find_graph_dependents()
                    for db_label in demand_database_names
                ]
            )

            if demand_database_last:
                database_names = [
                    x for x in database_names if x not in demand_database_names
                ] + demand_database_names

            data_objs.extend([Database(obj).datapackage() for obj in database_names])

            if remapping:
                # This is technically wrong - we could have more complicated queries
                # to determine what is truly a product, activity, etc.
                # However, for the default database schema, we know that each node
                # has a unique ID, so this won't produce incorrect responses,
                # just too many values. As the dictionary only exists once, this is
                # not really a problem.
                reversed_mapping = {
                    i: (d, c)
                    for d, c, i in AD.select(AD.database, AD.code, AD.id)
                    .where(AD.database << database_names)
                    .tuples()
                }
                remapping_dicts = {
                    "activity": reversed_mapping,
                    "product": reversed_mapping,
                    "biosphere": reversed_mapping,
                }

        if method:
            assert method in methods
            data_objs.append(Method(method).datapackage())
        if weighting:
            assert weighting in weightings
            data_objs.append(Weighting(weighting).datapackage())
        if normalization:
            assert normalization in normalizations
            data_objs.append(Normalization(normalization).datapackage())

        if demands:
            indexed_demand = [{get_id(k): v for k, v in dct.items()} for dct in demands]
        elif demand:
            indexed_demand = {get_id(k): v for k, v in demand.items()}
        else:
            indexed_demand = None

        return indexed_demand, data_objs, remapping_dicts

    def prepare_bw_timex_inputs(
        self,
        demand=None,
        method=None,
        weighting=None,
        normalization=None,
        demands=None,
        remapping=True,
        demand_database_last=True,
    ) -> tuple:
        """
        Prepare LCA input arguments in Brightway 2.5 style.

        ORIGINALLY FROM bw2data.compat.py

        Changes include:
        - always load all databases in demand_database_names
        - indexed_demand has the id of the new consumer_id of the "exploded" demand

        Parameters
        ----------
        demand : dict[object: float]
            The demand for which the LCA will be calculated. The keys can be Brightway `Node`
            instances, `(database, code)` tuples, or integer ids.
        method : tuple
            Tuple defining the LCIA method, such as `('foo', 'bar')`. Only needed if not passing
            `data_objs`.
        demand_timing_dict: dict
            Dictionary mapping demand ids to their timing.
        weighting : tuple
            Tuple defining the LCIA weighting, such as `('foo', 'bar')`. Only needed if not passing
            `data_objs`.
        normalization: str
        demands: list of dicts of demands
        remapping: bool
            If True, remap dictionaries
        demand_database_last: bool
            If True, add the demand databases last in the list `database_names`.

        Returns
        -------
        tuple
            Indexed demand, data objects, and remapping dictionaries

        See also
        --------
        bw2data.compat.prepare_lca_inputs: Original code this function is adapted from (https://github.com/brightway-lca/brightway2-data/blob/main/bw2data/compat.py).
        """

        if not projects.dataset.data.get("25"):
            raise Brightway2Project(
                "Please use `projects.migrate_project_25` before calculating using Brightway 2.5"
            )

        databases.clean()
        data_objs = []
        remapping_dicts = None

        demand_database_names = list(self.database_date_dict.keys())

        if demand_database_names:
            database_names = set.union(
                *[
                    Database(db_label).find_graph_dependents()
                    for db_label in demand_database_names
                ]
            )

            if demand_database_last:
                database_names = [
                    x for x in database_names if x not in demand_database_names
                ] + demand_database_names

            data_objs.extend([Database(obj).datapackage() for obj in database_names])

            if remapping:
                # This is technically wrong - we could have more complicated queries
                # to determine what is truly a product, activity, etc.
                # However, for the default database schema, we know that each node
                # has a unique ID, so this won't produce incorrect responses,
                # just too many values. As the dictionary only exists once, this is
                # not really a problem.
                reversed_mapping = {
                    i: (d, c)
                    for d, c, i in AD.select(AD.database, AD.code, AD.id)
                    .where(AD.database << database_names)
                    .tuples()
                }
                remapping_dicts = {
                    "activity": reversed_mapping,
                    "product": reversed_mapping,
                    "biosphere": reversed_mapping,
                }

        if method:
            assert method in methods
            data_objs.append(Method(method).datapackage())
        if weighting:
            assert weighting in weightings
            data_objs.append(Weighting(weighting).datapackage())
        if normalization:
            assert normalization in normalizations
            data_objs.append(Normalization(normalization).datapackage())

        if demands:
            indexed_demand = [
                {
                    self.activity_time_mapping_dict[
                        (
                            bd.get_node(id=bd.get_id(k)).key,
                            self.demand_timing_dict[bd.get_id(k)],
                        )
                    ]: v
                    for k, v in dct.items()
                }
                for dct in demands
            ]
        elif demand:
            indexed_demand = {
                self.activity_time_mapping_dict[
                    (
                        bd.get_node(id=bd.get_id(k)).key,
                        self.demand_timing_dict[bd.get_id(k)],
                    )
                ]: v
                for k, v in demand.items()
            }
        else:
            indexed_demand = None

        return indexed_demand, data_objs, remapping_dicts

    def create_node_id_collection_dict(self) -> None:
        """
        Creates a dict of collections of nodes that will be useful down the line, e.g. to determine static nodes for the graph traversal or create the dynamic biosphere matrix.
        Available collections are:

        - ``demand_database_names``: set of database names of the demand processes
        - ``demand_dependent_database_names``: set of database names of all processes that depend on the demand processes
        - ``demand_dependent_background_database_names``: set of database names of all processes that depend on the demand processes and are in the background databases
        - ``demand_dependent_background_node_ids``: set of node ids of all processes that depend on the demand processes and are in the background databases
        - ``foreground_node_ids``: set of node ids of all processes that are not in the background databases
        - ``first_level_background_node_ids_static``: set of node ids of all processes that are in the background databases and are directly linked to the demand processes
        - ``first_level_background_node_ids_all``: like first_level_background_node_ids_static, but includes first level background processes from other time explicit databases.
        - ``first_level_background_node_id_dbs``: dictionary with the first_level_background_node_ids_static as keys returning their database

        It also initiates an instance of SetList which contains all mappings of equivalent activieties across time-specific databases.
        - ``self.interdatabase_activity_mapping``: instance of SetList
        ----------
            None

        Returns
        -------
            None, but adds the `node_id_collection_dict containing` the above-mentioned collections, as well as interdatabase_activity_mapping
        """
        self.node_id_collection_dict = {}
        self.interdatabase_activity_mapping = SetList()

        # Original variable names preserved, set types for performance and uniqueness
        demand_database_names = {
            db
            for db in self.database_date_dict.keys()
            if db not in self.database_date_dict_static_only.keys()
        }
        self.node_id_collection_dict["demand_database_names"] = demand_database_names

        demand_dependent_database_names = set()
        for db in demand_database_names:
            demand_dependent_database_names.update(bd.Database(db).find_dependents())
        self.node_id_collection_dict["demand_dependent_database_names"] = (
            demand_dependent_database_names
        )

        demand_dependent_background_database_names = (
            demand_dependent_database_names & self.database_date_dict_static_only.keys()
        )
        self.node_id_collection_dict["demand_dependent_background_database_names"] = (
            demand_dependent_background_database_names
        )

        demand_dependent_background_node_ids = {
            node.id
            for db in demand_dependent_background_database_names
            for node in bd.Database(db)
        }
        self.node_id_collection_dict["demand_dependent_background_node_ids"] = (
            demand_dependent_background_node_ids
        )

        foreground_node_ids = {
            node.id
            for db in self.database_date_dict.keys()
            if db not in self.database_date_dict_static_only.keys()
            for node in bd.Database(db)
        }
        self.node_id_collection_dict["foreground_node_ids"] = foreground_node_ids

        first_level_background_node_ids_static = set()
        first_level_background_node_ids_all = set()
        for node_id in foreground_node_ids:
            node = bd.get_node(id=node_id)
            for exc in chain(node.technosphere(), node.substitution()):
                if exc.input["database"] in self.database_date_dict_static_only.keys():
                    first_level_background_node_ids_static.add(exc.input.id)
                    act_set = {(exc.input.id, exc.input["database"])}

                    for background_db in self.database_date_dict_static_only.keys():
                        try:
                            other_node = bd.get_node(
                                **{
                                    "database": background_db,
                                    "name": exc.input["name"],
                                    "product": exc.input["reference product"],
                                    "location": exc.input["location"],
                                }
                            )
                            first_level_background_node_ids_all.add(other_node.id)
                            act_set.add((other_node.id, background_db))
                        except KeyError as e:
                            warnings.warn(
                                f"Failed to find process in database {background_db} for name='{exc.input['name']}', reference product='{exc.input['reference product']}', location='{exc.input['location']}': {e}"
                            )
                    self.interdatabase_activity_mapping.add(act_set)

        self.node_id_collection_dict["first_level_background_node_ids_static"] = (
            first_level_background_node_ids_static
        )
        self.node_id_collection_dict["first_level_background_node_ids_all"] = (
            first_level_background_node_ids_all
        )

    def collect_temporalized_processes_from_timeline(self) -> None:
        """
        Prepares the input for the LCA from the timeline.

        Parameters
        ----------
        None

        Returns
        -------
        None, but adds "temporal_markets" and "temporalized_processes" to the
        node_id_colletion_dict based on the timeline.

        """
        unique_producers = (
            self.timeline.groupby(["producer", "time_mapped_producer"])
            .count()
            .index.values
        )

        temporal_market_ids = set()
        temporalized_process_ids = set()

        for producer, time_mapped_producer in unique_producers:
            if (
                bd.get_activity(producer)["database"]
                in self.database_date_dict_static_only.keys()
            ):
                temporal_market_ids.add(time_mapped_producer)
            else:
                temporalized_process_ids.add(time_mapped_producer)

        self.node_id_collection_dict["temporal_markets"] = temporal_market_ids
        self.node_id_collection_dict["temporalized_processes"] = (
            temporalized_process_ids
        )

    def add_static_activities_to_time_mapping_dict(self) -> None:
        """
        Adds all activities from the static LCA to `activity_time_mapping_dict`, an instance of `TimeMappingDict`.
        This gives a unique mapping in the form of (('database', 'code'), datetime_as_integer): time_mapping_id)
        that is later used to uniquely identify time-resolved processes. This is the pre-population of the time
        mapping dict with the static activities. Further time-explicit activities (from other temporalized
        background databases) are added lateron by in the TimelineBuilder.

        Parameters
        ----------
        None

        Returns
        -------
        None but adds the activities to the `activity_time_mapping_dict`
        """
        for idx in self.static_lca.dicts.activity.keys():  # activity ids
            key = self.static_lca.remapping_dicts["activity"][
                idx
            ]  # ('database', 'code')
            time = self.database_date_dict[
                key[0]
            ]  # datetime (or 'dynamic' for foreground processes)
            if isinstance(time, str):  # if 'dynamic', just add the string
                self.activity_time_mapping_dict.add((key, time), unique_id=idx)
            elif isinstance(time, datetime):
                self.activity_time_mapping_dict.add(
                    (key, extract_date_as_integer(time, self.temporal_grouping)),
                    unique_id=idx,
                )  # if datetime, map to the date as integer
            else:
                warnings.warn(f"Time of activity {key} is neither datetime nor str.")

    def create_demand_timing_dict(self) -> dict:
        """
        Generate a dictionary that maps producer (key) to timing (value) for the demands in the product system.
        It searches the timeline for those rows that contain the functional units (demand-processes as producer and -1 as consumer) and returns the time of the demand as an integer.
        Time of demand can have flexible resolution (year=YYYY, month=YYYYMM, day=YYYYMMDD, hour=YYYYMMDDHH) defined in `temporal_grouping`.

        Parameters
        ----------
        None

        Returns
        -------
        Dictionary mapping producer ids to reference timing for the specified demands.
        """
        demand_ids = [bd.get_activity(key).id for key in self.demand.keys()]
        demand_rows = self.timeline[
            self.timeline["producer"].isin(demand_ids)
            & (self.timeline["consumer"] == -1)
        ]
        self.demand_timing_dict = {
            row.producer: row.hash_producer for row in demand_rows.itertuples()
        }
        return self.demand_timing_dict

    ######################################
    # For creating human-friendly output #
    ######################################

    def create_labelled_technosphere_dataframe(self) -> pd.DataFrame:
        """
        Returns the technosphere matrix as a dataframe with comprehensible labels instead of ids.

        Parameters
        ----------
        None

        Returns
        -------
        pd.DataFrame, technosphere matrix as a pandas.DataFrame with comprehensible labels instead of ids.
        """

        df = pd.DataFrame(self.technosphere_matrix.toarray())
        df.rename(  # from matrix id to activity id
            index=self.dicts.activity.reversed,
            columns=self.dicts.activity.reversed,
            inplace=True,
        )
        df.rename(  # from activity id to ((database, code), time)
            index=self.activity_time_mapping_dict.reversed(),
            columns=self.activity_time_mapping_dict.reversed(),
            inplace=True,
        )
        return df

    def create_labelled_biosphere_dataframe(self) -> pd.DataFrame:
        """
        Returns the biosphere matrix as a pandas.DataFrame with comprehensible labels instead of ids.

        Parameters
        ----------
        None

        Returns
        -------
        pd.DataFrame, biosphere matrix as a pandas.DataFrame with comprehensible labels instead of ids.
        """

        df = pd.DataFrame(self.biosphere_matrix.toarray())
        df.rename(  # from matrix id to activity id
            index=self.dicts.biosphere.reversed,
            columns=self.dicts.activity.reversed,
            inplace=True,
        )
        df.rename(
            index=self.remapping_dicts["biosphere"],  # from activity id to bioflow name
            columns=self.activity_time_mapping_dict.reversed(),  # from activity id to ((database, code), time)
            inplace=True,
        )

        return df

    def create_labelled_dynamic_biosphere_dataframe(self) -> pd.DataFrame:
        """
        Returns the dynamic biosphere matrix as a dataframe with comprehensible labels instead of ids.

        Parameters
        ----------
        None

        Returns
        -------
        pd.DataFrame, dynamic biosphere matrix as a pandas.DataFrame with comprehensible labels instead of ids.
        """
        df = pd.DataFrame(self.dynamic_biomatrix.toarray())
        df.rename(  # from matrix id to activity id
            index=self.biosphere_time_mapping_dict_reversed,
            columns=self.dicts.activity.reversed,
            inplace=True,
        )
        df.rename(  # from activity id to ((database, code), time)
            columns=self.activity_time_mapping_dict.reversed(),
            inplace=True,
        )

        df = df.loc[(df != 0).any(axis=1)]  # For readablity, remove all-zero rows

        return df

    def plot_dynamic_inventory(self, bio_flows, cumulative=False) -> None:
        """
        Simple plot of dynamic inventory of a biosphere flow over time, with optional cumulative plotting.

        Parameters
        ----------
        bio_flows : list of int
            database ids of the biosphere flows to plot.
        cumulative : bool
            if True, plot cumulative amounts over time

        Returns
        -------
        none, but shows a plot
        """
        plt.figure(figsize=(14, 6))

        filtered_df = self.dynamic_inventory_df[
            self.dynamic_inventory_df["flow"].isin(bio_flows)
        ]
        aggregated_df = filtered_df.groupby("date").sum()["amount"].reset_index()

        if cumulative:
            aggregated_df["amount"] = np.cumsum(aggregated_df["amount"])

        plt.plot(
            aggregated_df["date"], aggregated_df["amount"], marker="o", linestyle="none"
        )

        plt.ylim(bottom=0)
        plt.xlabel("time")
        plt.ylabel("amount [kg]")
        plt.grid(True)
        plt.tight_layout()  # Adjust layout to make room for the rotated date labels
        plt.show()

    def plot_dynamic_characterized_inventory(
        self,
        cumsum: bool = False,
        sum_emissions_within_activity: bool = False,
        sum_activities: bool = False,
    ) -> None:
        """
        Plot the characterized inventory of the dynamic LCI in a very simple plot.
        Legend and title are selcted automatically based on the chosen metric.

        Parameters
        ----------
        cumsum : bool
            if True, plot cumulative amounts over time
        sum_emissions_within_activity : bool
            if True, sum emissions within each activity over time
        sum_activities : bool
            if True, sum emissions over all activities over time

        Returns
        -------
        none, but shows a plot
        """

        if not hasattr(self, "characterized_inventory"):
            warnings.warn(
                "Characterized inventory not yet calculated. Call TimexLCA.dynamic_lcia() first."
            )
            return

        # Fetch the inventory to use in plotting, modify based on flags
        plot_data = self.characterized_inventory.copy()

        if cumsum:
            plot_data["amount_sum"] = plot_data["amount"].cumsum()
            amount = "amount_sum"
        else:
            amount = "amount"

        if sum_emissions_within_activity:
            plot_data = plot_data.groupby(["date", "activity"]).sum().reset_index()
            plot_data["amount_sum"] = plot_data["amount"].cumsum()

        if sum_activities:
            plot_data = plot_data.groupby("date").sum().reset_index()
            plot_data["amount_sum"] = plot_data["amount"].cumsum()
            plot_data["activity_label"] = "All activities"

        else:  # plotting activities separate

            activity_name_cache = {}

            for activity in plot_data["activity"].unique():
                if activity not in activity_name_cache:
                    activity_name_cache[activity] = bd.get_activity(
                        self.activity_time_mapping_dict_reversed[activity][0]
                    )["name"]

            plot_data["activity_label"] = plot_data["activity"].map(activity_name_cache)

        # Plotting
        plt.figure(figsize=(14, 6))
        axes = sb.scatterplot(x="date", y=amount, hue="activity_label", data=plot_data)

        # Determine y-axis limit flexibly
        if plot_data[amount].min() < 0:
            ymin = plot_data[amount].min() * 1.1
        else:
            ymin = 0

        axes.set_axisbelow(True)
        axes.set_ylim(bottom=ymin)
        axes.set_xlabel("Time")

        handles, labels = axes.get_legend_handles_labels()
        axes.legend(handles[::-1], labels[::-1])
        plt.grid(True)
        plt.show()

    #########
    # Other #
    #########

    def remap_inventory_dicts(self) -> None:
        """
        Give a warning if users want to use bw25's original mapping function.

        Parameters
        ----------
        None

        Returns
        -------
        None, but gives a warning

        """
        warnings.warn(
            "bw25's original mapping function doesn't work with our new time-mapped matrix entries. The Timex mapping can be found in acvitity_time_mapping_dict and biosphere_time_mapping_dict."
        )

    def __getattr__(self, name):
        """
        Delegate attribute access to the self.lca object if the attribute
        is not found in the TimexLCA instance itself, excluding special attributes.
        """
        if name.startswith("__"):
            raise AttributeError(
                f"'{type(self).__name__}' object has no attribute '{name}'"
            )
        if hasattr(self.lca, name):
            return getattr(self.lca, name)
        if hasattr(self.dynamic_biosphere_builder, name):
            return getattr(self.dynamic_biosphere_builder, name)
        else:
            raise AttributeError(
                f"'TimexLCA' object and its 'lca'- and dynamic_biosphere_builder- attributes have no attribute '{name}'"
            )
