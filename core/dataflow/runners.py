"""
Import as:

import core.dataflow.runners as cdtfr
"""

import abc
import logging
from typing import Any, Dict, Generator, List, Optional, Tuple

import pandas as pd

import core.config as cconfig
import core.dataflow.core as cdtfc
import core.dataflow.real_time as cdtfrt
import core.dataflow.utils as cdtfu
import core.dataflow.visitors as cdtfv
import helpers.datetime_ as hdatetime
import helpers.dbg as dbg

# TODO(gp): Use the standard imports.
from core.dataflow.builders import DagBuilder
from core.dataflow.result_bundle import PredictionResultBundle, ResultBundle
from core.dataflow.visitors import extract_info, set_fit_state

_LOG = logging.getLogger(__name__)


# TODO(gp): Should we call the `start` params -> `start_datetime`

# #############################################################################


class _AbstractDagRunner(abc.ABC):
    """
    Abstract class with the common code to all `DagRunner`s.

    There is not a method common to all `DagRunner`s that is abstract,
    so we use `abc.ABC` to guarantee that this class is not instantiated
    directly.
    """

    def __init__(self, config: cconfig.Config, dag_builder: DagBuilder) -> None:
        """
        Constructor.

        :param config: config for DAG
        :param dag_builder: `DagBuilder` instance to build a DAG from the config
        """
        # Save input parameters.
        self.config = config
        self._dag_builder = dag_builder
        # Build DAG using DAG builder.
        # TODO(gp): Now a DagRunner builds and runs a DAG. This creates some coupling.
        #  Consider having a DagRunner accept a DAG however built and run it.
        self.dag = self._dag_builder.get_dag(self.config)
        _LOG.debug("dag=%s", self.dag)
        # Check that the DAG has the required methods.
        methods = self._dag_builder.methods
        _LOG.debug("methods=%s", methods)
        dbg.dassert_in("fit", methods)
        dbg.dassert_in("predict", methods)
        # Get the mapping from columns to tags.
        self._column_to_tags_mapping = (
            self._dag_builder.get_column_to_tags_mapping(self.config)
        )
        _LOG.debug("_column_to_tags_mapping=%s", self._column_to_tags_mapping)
        # Extract the sink node.
        self._result_nid = self.dag.get_unique_sink()
        _LOG.debug("_result_nid=%s", self._result_nid)

    def _set_fit_predict_intervals(
        self, method: cdtfc.Method, intervals: Optional[cdtfu.Intervals]
    ) -> None:
        """
        Set fit or predict intervals for all the source nodes.

        :param intervals: as in `DataSource` node, but allowing `None` to mean no
            interval
        """
        if intervals is None:
            return
        # Propagate the intervals to all source nodes.
        for input_nid in self.dag.get_sources():
            node = self.dag.get_node(input_nid)
            if method == "fit":
                node.set_fit_intervals(intervals)
            elif method == "predict":
                node.set_predict_intervals(intervals)
            else:
                raise ValueError("Invalid method='%s'" % method)

    def _run_dag_helper(
        self, method: cdtfc.Method
    ) -> Tuple[pd.DataFrame, cdtfv.NodeInfo]:
        """
        Run the DAG for the given method.

        :return: the dataframe for the only output and the associated Info
        """
        nid = self._result_nid
        # TODO(gp): Add a check for `df_out`.
        df_out = self.dag.run_leq_node(nid, method)["df_out"]
        info = extract_info(self.dag, [method])
        return df_out, info

    # TODO(gp): This could be folded into `_run_dag_helper()` if we collapse
    #  `ResultBundle` and `PredictionResultBundle`.
    def _to_result_bundle(
        self, method: cdtfc.Method, df_out: pd.DataFrame, info: cdtfv.NodeInfo
    ) -> ResultBundle:
        """
        Package the result of a DAG execution into a ResultBundle.
        """
        return ResultBundle(
            config=self.config,
            result_nid=self._result_nid,
            method=method,
            result_df=df_out,
            column_to_tags=self._column_to_tags_mapping,
            info=info,
        )


# #############################################################################


class FitPredictDagRunner(_AbstractDagRunner):
    """
    Run DAGs that have fit / predict methods.
    """

    def set_fit_intervals(self, intervals: Optional[cdtfu.Intervals]) -> None:
        """
        Set fit intervals for all the source nodes.

        :param intervals: as in `DataSource` node, but allowing `None`
        """
        method = "fit"
        self._set_fit_predict_intervals(method, intervals)

    def set_predict_intervals(self, intervals: Optional[cdtfu.Intervals]) -> None:
        """
        Set predict intervals for all the source nodes.

        :param intervals: as in `DataSource` node, but allowing `None`
        """
        method = "predict"
        self._set_fit_predict_intervals(method, intervals)

    def fit(self) -> ResultBundle:
        """
        Fitting means running `fit()` method on the DAG up to the sink node.
        """
        method = "fit"
        return self._run_dag(method)

    def predict(self) -> ResultBundle:
        """
        Predicting means running `predict()` method on the DAG up to the sink
        node.
        """
        method = "predict"
        return self._run_dag(method)

    def _run_dag(self, method: cdtfc.Method) -> ResultBundle:
        df_out, info = self._run_dag_helper(method)
        return self._to_result_bundle(method, df_out, info)


# #############################################################################


class PredictionDagRunner(FitPredictDagRunner):
    """
    Run prediction DAGs.

    Identical to `FitPredictDagRunner`, but returning a
    `PredictionResultBundle`.
    """

    def _run_dag(self, method: cdtfc.Method) -> PredictionResultBundle:
        """
        Same as super class but return a `PredictionResultBundle`.
        """
        df_out, info = self._run_dag_helper(method)
        return PredictionResultBundle(
            config=self.config,
            result_nid=self._result_nid,
            method=method,
            result_df=df_out,
            column_to_tags=self._column_to_tags_mapping,
            info=info,
        )


# #############################################################################


class RollingFitPredictDagRunner(_AbstractDagRunner):
    """
    Run a DAG by periodic fitting on previous history and evaluating on new
    data.
    """

    def __init__(
        self,
        config: cconfig.Config,
        dag_builder: DagBuilder,
        start: hdatetime.Datetime,
        end: hdatetime.Datetime,
        retraining_freq: str,
        retraining_lookback: int,
    ) -> None:
        """
        Constructor.

        :param start: start of available data for use in training
        :param end: end of available data for use in training
        :param retraining_freq: how often to retrain using Pandas frequency
            convention (e.g., `2B`)
        :param retraining_lookback: number of periods of past data to include
            in retraining, expressed in integral units of `retraining_freq`
        """
        super().__init__(config, dag_builder)
        # Save input parameters.
        self._start = start
        self._end = end
        self._retraining_freq = retraining_freq
        self._retraining_lookback = retraining_lookback
        # Generate retraining dates.
        self._retraining_datetimes = self._generate_retraining_datetimes(
            start=self._start,
            end=self._end,
            retraining_freq=self._retraining_freq,
            retraining_lookback=self._retraining_lookback,
        )
        _LOG.info("_retraining_datetimes=%s", self._retraining_datetimes)

    def fit_predict(self) -> Generator:
        """
        Fit at each retraining date and predict until next retraining date.

        :return: the training time, fit `ResultBundle`, predict `ResultBundle`
        """
        for training_datetime in self._retraining_datetimes:
            (
                fit_result_bundle,
                predict_result_bundle,
            ) = self.fit_predict_at_datetime(training_datetime)
            # TODO(gp): Better to return a pd.Timestamp rather than its representation.
            training_datetime_str = training_datetime.strftime("%Y%m%d_%H%M%S")
            yield training_datetime_str, fit_result_bundle, predict_result_bundle

    def fit_predict_at_datetime(
        self,
        datetime_: hdatetime.Datetime,
    ) -> Tuple[ResultBundle, ResultBundle]:
        """
        Fit with all the history up and including `datetime` and then predict
        forward.

        :param datetime_: point in time at which to train (historically) and then
            predict (one step ahead)
        :return: populated fit and predict `ResultBundle`s
        """
        # Determine fit interval.
        idx = pd.date_range(
            end=datetime_,
            freq=self._retraining_freq,
            periods=self._retraining_lookback,
        )
        start_datetime = idx[0]
        fit_interval = (start_datetime, datetime_)
        # Fit in the interval [start_datetime, datetime_].
        fit_result_bundle = self._run_fit(fit_interval)
        # Determine predict interval.
        idx = idx.shift(freq=self._retraining_freq)
        end_datetime = idx[-1]
        predict_interval = (start_datetime, end_datetime)
        # Predict in the interval [datetime_ + \epsilon, end_datetime].
        predict_result_bundle = self._run_predict(predict_interval, datetime_)
        return fit_result_bundle, predict_result_bundle

    # TODO(gp): -> _fit for symmetry with the rest of the code.
    def _run_fit(
        self, interval: Tuple[hdatetime.Datetime, hdatetime.Datetime]
    ) -> ResultBundle:
        # Set fit interval on all source nodes of the DAG.
        for input_nid in self.dag.get_sources():
            node = self.dag.get_node(input_nid)
            node.set_fit_intervals([interval])
        # Fit.
        method = "fit"
        df_out, info = self._run_dag_helper(method)
        return self._to_result_bundle(method, df_out, info)

    def _run_predict(
        self,
        # TODO(gp): Use Interval
        interval: Tuple[hdatetime.Datetime, hdatetime.Datetime],
        oos_start: hdatetime.Datetime,
    ) -> ResultBundle:
        # Set predict interval on all source nodes of the DAG.
        for input_nid in self.dag.get_sources():
            self.dag.get_node(input_nid).set_predict_intervals([interval])
        # Predict.
        method = "predict"
        df_out, info = self._run_dag_helper(method)
        # Restrict `df_out` to out-of-sample portion.
        df_out = df_out.loc[oos_start:]
        return self._to_result_bundle(method, df_out, info)

    @staticmethod
    def _generate_retraining_datetimes(
        start: hdatetime.Datetime,
        end: hdatetime.Datetime,
        retraining_freq: str,
        retraining_lookback: int,
    ) -> pd.DatetimeIndex:
        """
        Generate an index of retraining dates based on specs.

        The input parameters have the same meaning as in the constructor.

        :return: (re)training dates
        """
        # Populate an initial index of candidate retraining dates.
        grid = pd.date_range(start=start, end=end, freq=retraining_freq)
        # The ability to compute a nonempty `idx` is the first sanity-check.
        dbg.dassert(
            not grid.empty, msg="Not enough data for requested training schedule!"
        )
        dbg.dassert_isinstance(retraining_lookback, int)
        # Ensure that `grid` has enough lookback points.
        dbg.dassert_lt(
            retraining_lookback,
            grid.size,
            msg="Input data does not have %i periods" % retraining_lookback,
        )
        # Shift the start of the index by the lookback amount. The new start
        # represents the first data point that will be used in (the first)
        # training.
        lookback = str(retraining_lookback) + retraining_freq
        idx = grid.shift(freq=lookback)
        # Trim end of date range back to `end`. This is needed because the
        # previous `shift()` also moves the end of the index.
        # TODO(Paul): Add back `end` for a fit-only step.
        idx = idx[idx < end]
        #
        dbg.dassert(not idx.empty)
        return idx

    def _run_dag(self, method: cdtfc.Method) -> ResultBundle:
        """
        Run DAG and return a ResultBundle.
        """
        df_out, info = self._run_dag_helper(method)
        return self._to_result_bundle(method, df_out, info)


# #############################################################################


class IncrementalDagRunner(_AbstractDagRunner):
    """
    Run DAGs in incremental fashion, i.e., running one step at a time.

    # TODO(gp): Improve description.
    """

    def __init__(
        self,
        config: cconfig.Config,
        dag_builder: DagBuilder,
        start: hdatetime.Datetime,
        end: hdatetime.Datetime,
        freq: str,
        fit_state: cconfig.Config,
    ) -> None:
        """
        Constructor.

        :param config: config for DAG
        :param dag_builder: `DagBuilder` instance
        :param start: first prediction datetime_ (e.g., first time at which we
            generate a prediction in `predict` mode, using all available data
            up to and including `start`)
        :param end: last prediction datetime_
        :param freq: prediction frequency (typically the same as the frequency
            of the underlying DAG)
        :param fit_state: Config containing any learned state required for
            initializing the DAG
        """
        super().__init__(config, dag_builder)
        self._start = start
        self._end = end
        self._freq = freq
        self._fit_state = fit_state
        set_fit_state(self.dag, self._fit_state)
        # Create predict range.
        self._date_range = pd.date_range(
            start=self._start, end=self._end, freq=self._freq
        )

    def predict(self) -> Generator:
        """
        Generate a filtration and predict at each index of the filtration.

        Here we use "filtration" as it is used in the context of stochastic
        processes. To ensure that predictions are non-anticipating, we restrict
        the model inputs to times up to and including the prediction time.

        :return: a generator of `ResultBundle`s (one `ResultBundle` for each
            prediction)
        """
        for end_dt in self._date_range:
            result_bundle = self.predict_at_datetime(end_dt)
            yield result_bundle

    # TODO(gp): dt -> datetime_ as used elsewhere.
    def predict_at_datetime(self, dt: hdatetime.Datetime) -> ResultBundle:
        """
        Generate a prediction as of `dt` (for a future point in time).

        :param dt: point in time at which to generate a prediction
        :return: populated `ResultBundle`
        """
        # Cut off data at `end_dt`. Do not restrict the start datetime_ so
        # so as not to adversely affect any required warm-up period.
        interval = [(None, dt)]
        # Set prediction intervals and predict.
        for input_nid in self.dag.get_sources():
            self.dag.get_node(input_nid).set_predict_intervals(interval)
        result_bundle = self._run_dag("predict")
        return result_bundle

    def _run_dag(self, method: cdtfc.Method) -> ResultBundle:
        """
        Run DAG and return a ResultBundle.
        """
        df_out, info = self._run_dag_helper(method)
        return self._to_result_bundle(method, df_out, info)


# #############################################################################


class RealTimeDagRunner(_AbstractDagRunner):
    """
    Run a DAG in true or simulated real-time.

    A DAG can be executed in real-time if one of the source nodes has a notion of
    time and emits different data for the same invocation because time has elapsed.

    See:
    - `real_time.py` for definitions of different types of real-time executions
    - `_AbstractRealTimeDataSource` and descendants for nodes with real-time semantic
    """

    def __init__(
        self,
        config: cconfig.Config,
        dag_builder: DagBuilder,
        fit_state: cconfig.Config,
        execute_rt_loop_kwargs: Dict[str, Any],
        dst_dir: str,
    ) -> None:
        super().__init__(config, dag_builder)
        # Save input parameters.
        # TODO(gp): Use this for stateful DAGs.
        _ = fit_state
        self._execute_rt_loop_kwargs = execute_rt_loop_kwargs
        self._dst_dir = dst_dir
        # Store information about the real-time execution.
        self._events: cdtfrt.Events = []

    async def predict(self) -> List[ResultBundle]:
        """
        Execute the DAG until there are events.

        This adapts the asynchronous generator to a synchronous semantic.
        """
        result_bundles = [result_bundle async for result_bundle in self.predict_at_datetime()]
        return result_bundles

    async def predict_at_datetime(self) -> ResultBundle:
        """
        Predict every time there is a real-time event.

        :return: list of `ResultBundle`, one per event
        """
        method = "predict"
        # Adapt `_dag_workload()` to the expected call back signature.
        workload = lambda current_time: self._run_dag(method)
        # Call the event loop.
        async for event, result_bundle in cdtfrt.execute_with_real_time_loop(
            **self._execute_rt_loop_kwargs, workload=workload
        ):
            self._events.append(event)
            yield result_bundle

    @property
    def events(self) -> Optional[cdtfrt.Events]:
        return self._events

    async def _run_dag(self, method: cdtfc.Method) -> ResultBundle:
        df_out, info = self._run_dag_helper(method)
        return self._to_result_bundle(method, df_out, info)
