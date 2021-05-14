import collections
import datetime
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import gluonts.model.deepar as gmdeep
import gluonts.trainer as gtrain
import numpy as np
import pandas as pd
import scipy as sp
import sklearn as sklear
import statsmodels.api as sm
import statsmodels.iolib as siolib
from tqdm.autonotebook import tqdm

import core.backtest as cbackt
import core.config as cconfi
import core.config_builders as ccbuild
import core.data_adapters as cdataa
import core.signal_processing as csigna
import core.statistics as cstati
import helpers.dbg as dbg
from core.dataflow.core import DAG, Node
from core.dataflow.nodes import (
    ColModeMixin,
    ColumnTransformer,
    DataframeMethodRunner,
    FitPredictNode,
    ReadDataFromDf,
    get_df_info_as_string,
)
from core.dataflow.visitors import extract_info

_LOG = logging.getLogger(__name__)


_COL_TYPE = Union[int, str]
_PANDAS_DATE_TYPE = Union[str, pd.Timestamp, datetime.datetime]
_TO_LIST_MIXIN_TYPE = Union[List[_COL_TYPE], Callable[[], List[_COL_TYPE]]]


# #############################################################################
# Model Mixins
# #############################################################################


class RegFreqMixin:
    """
    Requires input dataframe to have a well-defined frequency and unique cols.
    """

    @staticmethod
    def _validate_input_df(df: pd.DataFrame) -> None:
        """
        Assert if df violates constraints, otherwise return `None`.
        """
        dbg.dassert_isinstance(df, pd.DataFrame)
        dbg.dassert_no_duplicates(df.columns.tolist())
        dbg.dassert(df.index.freq)


class ToListMixin:
    """
    Supports callables that return lists.
    """

    @staticmethod
    def _to_list(to_list: _TO_LIST_MIXIN_TYPE) -> List[_COL_TYPE]:
        """
        Return a list given its input.

        - If the input is a list, the output is the same list.
        - If the input is a function that returns a list, then the output of
          the function is returned.

        How this might arise in practice:
          - A ColumnTransformer returns a number of x variables, with the
            number dependent upon a hyperparameter expressed in config
          - The column names of the x variables may be derived from the input
            dataframe column names, not necessarily known until graph execution
            (and not at construction)
          - The ColumnTransformer output columns are merged with its input
            columns (e.g., x vars and y vars are in the same DataFrame)
        Post-merge, we need a way to distinguish the x vars and y vars.
        Allowing a callable here allows us to pass in the ColumnTransformer's
        method `transformed_col_names` and defer the call until graph
        execution.
        """
        if callable(to_list):
            to_list = to_list()
        if isinstance(to_list, list):
            return to_list
        raise TypeError("Data type=`%s`" % type(to_list))


# #############################################################################
# sklearn - supervised prediction models
# #############################################################################


class ContinuousSkLearnModel(
    FitPredictNode, RegFreqMixin, ToListMixin, ColModeMixin
):
    """
    Fit and predict an sklearn model.
    """

    # pylint: disable=too-many-ancestors

    def __init__(
        self,
        nid: str,
        model_func: Callable[..., Any],
        x_vars: _TO_LIST_MIXIN_TYPE,
        y_vars: _TO_LIST_MIXIN_TYPE,
        steps_ahead: int,
        model_kwargs: Optional[Any] = None,
        col_mode: Optional[str] = None,
        nan_mode: Optional[str] = None,
    ) -> None:
        """
        Specify the data and sklearn modeling parameters.

        :param nid: unique node id
        :param model_func: an sklearn model
        :param x_vars: indexed by knowledge datetimes
            - `x_vars` may contain lags of `y_vars`
        :param y_vars: indexed by knowledge datetimes
            - e.g., in the case of returns, this would correspond to `ret_0`
        :param steps_ahead: number of steps ahead for which a prediction is
            to be generated. E.g.,
            - if `steps_ahead == 0`, then the predictions are
              are contemporaneous with the observed response (and hence
              inactionable)
            - if `steps_ahead == 1`, then the model attempts to predict
              `y_vars` for the next time step
            - The model is only trained to predict the target
              `steps_ahead` steps ahead (and not all intermediate steps)
        :param model_kwargs: parameters to forward to the sklearn model
            (e.g., regularization constants)
        :param col_mode: "merge_all" or "replace_all", as in
            ColumnTransformer()
        :param nan_mode: "drop" or "raise"
        """
        super().__init__(nid)
        self._model_func = model_func
        self._model_kwargs = model_kwargs or {}
        self._x_vars = x_vars
        self._y_vars = y_vars
        self._model = None
        self._steps_ahead = steps_ahead
        dbg.dassert_lte(
            0, self._steps_ahead, "Non-causal prediction attempted! Aborting..."
        )
        # NOTE: Set to "replace_all" for backward compatibility.
        self._col_mode = col_mode or "replace_all"
        dbg.dassert_in(self._col_mode, ["replace_all", "merge_all"])
        self._nan_mode = nan_mode or "raise"

    def fit(self, df_in: pd.DataFrame) -> Dict[str, pd.DataFrame]:
        self._validate_input_df(df_in)
        df = df_in.copy()
        # Obtain index slice for which forward targets exist.
        dbg.dassert_lt(self._steps_ahead, df.index.size)
        idx = df.index[: -self._steps_ahead]
        # Determine index where no x_vars are NaN.
        x_vars = self._to_list(self._x_vars)
        non_nan_idx_x = df.loc[idx][x_vars].dropna().index
        # Determine index where target is not NaN.
        fwd_y_df = self._get_fwd_y_df(df).loc[idx].dropna()
        non_nan_idx_fwd_y = fwd_y_df.dropna().index
        # Intersect non-NaN indices.
        non_nan_idx = non_nan_idx_x.intersection(non_nan_idx_fwd_y)
        dbg.dassert(not non_nan_idx.empty)
        fwd_y_df = fwd_y_df.loc[non_nan_idx]
        # Handle presence of NaNs according to `nan_mode`.
        self._handle_nans(idx, non_nan_idx)
        # Prepare x_vars in sklearn format.
        x_fit = cdataa.transform_to_sklearn(df.loc[non_nan_idx], x_vars)
        # Prepare forward y_vars in sklearn format.
        fwd_y_fit = cdataa.transform_to_sklearn(
            fwd_y_df, fwd_y_df.columns.tolist()
        )
        # Define and fit model.
        self._model = self._model_func(**self._model_kwargs)
        self._model = self._model.fit(x_fit, fwd_y_fit)
        # Generate insample predictions and put in dataflow dataframe format.
        fwd_y_hat = self._model.predict(x_fit)
        #
        fwd_y_hat_vars = [f"{y}_hat" for y in fwd_y_df.columns]
        fwd_y_hat = cdataa.transform_from_sklearn(
            non_nan_idx, fwd_y_hat_vars, fwd_y_hat
        )
        # TODO(Paul): Summarize model perf or make configurable.
        # TODO(Paul): Consider separating model eval from fit/predict.
        info = collections.OrderedDict()
        info["model_x_vars"] = x_vars
        info["model_params"] = self._model.get_params()
        model_attribute_info = collections.OrderedDict()
        for k, v in vars(self._model).items():
            model_attribute_info[k] = v
        info["model_attributes"] = model_attribute_info
        info["insample_perf"] = self._model_perf(fwd_y_df, fwd_y_hat)
        info["insample_score"] = self._score(fwd_y_df, fwd_y_hat)
        # Return targets and predictions.
        df_out = fwd_y_df.merge(
            fwd_y_hat, how="outer", left_index=True, right_index=True
        )
        df_out = df_out.reindex(idx)
        df_out = self._apply_col_mode(
            df, df_out, cols=self._to_list(self._y_vars), col_mode=self._col_mode
        )
        info["df_out_info"] = get_df_info_as_string(df_out)
        self._set_info("fit", info)
        return {"df_out": df_out}

    def predict(self, df_in: pd.DataFrame) -> Dict[str, pd.DataFrame]:
        self._validate_input_df(df_in)
        df = df_in.copy()
        idx = df.index
        # Restrict to times where x_vars have no NaNs.
        x_vars = self._to_list(self._x_vars)
        non_nan_idx = df.loc[idx][x_vars].dropna().index
        # Handle presence of NaNs according to `nan_mode`.
        self._handle_nans(idx, non_nan_idx)
        # Transform x_vars to sklearn format.
        x_predict = cdataa.transform_to_sklearn(df.loc[non_nan_idx], x_vars)
        # Use trained model to generate predictions.
        dbg.dassert_is_not(
            self._model, None, "Model not found! Check if `fit` has been run."
        )
        fwd_y_hat = self._model.predict(x_predict)
        # Put predictions in dataflow dataframe format.
        fwd_y_df = self._get_fwd_y_df(df).loc[non_nan_idx]
        fwd_y_non_nan_idx = fwd_y_df.dropna().index
        fwd_y_hat_vars = [f"{y}_hat" for y in fwd_y_df.columns]
        fwd_y_hat = cdataa.transform_from_sklearn(
            non_nan_idx, fwd_y_hat_vars, fwd_y_hat
        )
        # Generate basic perf cstati.
        info = collections.OrderedDict()
        info["model_params"] = self._model.get_params()
        info["model_perf"] = self._model_perf(fwd_y_df, fwd_y_hat)
        info["model_score"] = self._score(
            fwd_y_df.loc[fwd_y_non_nan_idx], fwd_y_hat.loc[fwd_y_non_nan_idx]
        )
        # Return predictions.
        df_out = fwd_y_df.merge(
            fwd_y_hat, how="outer", left_index=True, right_index=True
        )
        df_out = df_out.reindex(idx)
        df_out = self._apply_col_mode(
            df, df_out, cols=self._to_list(self._y_vars), col_mode=self._col_mode
        )
        info["df_out_info"] = get_df_info_as_string(df_out)
        # TODO(*): Consider adding state to `info` as follows.
        # info["state"] = pickle.dumps(self._model)
        self._set_info("predict", info)
        return {"df_out": df_out}

    def get_fit_state(self) -> Dict[str, Any]:
        fit_state = {"_model": self._model, "_info['fit']": self._info["fit"]}
        return fit_state

    def set_fit_state(self, fit_state: Dict[str, Any]):
        self._model = fit_state["_model"]
        self._info["fit"] = fit_state["_info['fit']"]

    def _get_fwd_y_df(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Return dataframe of `steps_ahead` forward y values.
        """
        y_vars = self._to_list(self._y_vars)
        mapper = lambda y: str(y) + "_%i" % self._steps_ahead
        # TODO(Paul): Ensure that `fwd_y_vars` and `y_vars` do not overlap.
        fwd_y_df = df[y_vars].shift(-self._steps_ahead).rename(columns=mapper)
        return fwd_y_df

    def _handle_nans(
        self, idx: pd.DataFrame.index, non_nan_idx: pd.DataFrame.index
    ) -> None:
        if self._nan_mode == "raise":
            if idx.shape[0] != non_nan_idx.shape[0]:
                nan_idx = idx.difference(non_nan_idx)
                raise ValueError(f"NaNs detected at {nan_idx}")
        elif self._nan_mode == "drop":
            pass
        else:
            raise ValueError(f"Unrecognized nan_mode `{self._nan_mode}`")

    def _score(
        self,
        y_true: Union[pd.Series, pd.DataFrame],
        y_pred: Union[pd.Series, pd.DataFrame],
    ) -> Optional[float]:
        """
        Compute accuracy for classification or R^2 score for regression.
        """
        if sklear.base.is_classifier(self._model):
            metric = sklear.metrics.accuracy_score
        elif sklear.base.is_regressor(self._model):
            metric = sklear.metrics.r2_score
        else:
            return None
        # In `predict()` method, `y_pred` may exist for index where `y_true`
        # is already `NaN`.
        y_true = y_true.loc[: y_true.last_valid_index()]
        return metric(y_true, y_pred.loc[y_true.index])

    # TODO(Paul): Consider omitting this (and relying on downstream
    #     processing to e.g., adjust for number of hypotheses tested).
    @staticmethod
    def _model_perf(
        y: pd.DataFrame, y_hat: pd.DataFrame
    ) -> collections.OrderedDict:
        info = collections.OrderedDict()
        # info["hitrate"] = pip._compute_model_hitrate(self.model, x, y)
        pnl_rets = y.multiply(
            y_hat.rename(columns=lambda x: x.replace("_hat", ""))
        )
        info["pnl_rets"] = pnl_rets
        info["sr"] = cstati.compute_annualized_sharpe_ratio(
            csigna.resample(pnl_rets, rule="1B").sum()
        )
        return info


class SkLearnModel(FitPredictNode, ToListMixin, ColModeMixin):
    """
    Fit and predict an sklearn model.

    No NaN-handling or uniform sampling frequency requirement.
    """

    def __init__(
        self,
        nid: str,
        x_vars: _TO_LIST_MIXIN_TYPE,
        y_vars: _TO_LIST_MIXIN_TYPE,
        model_func: Callable[..., Any],
        model_kwargs: Optional[Any] = None,
        col_mode: Optional[str] = None,
    ) -> None:
        super().__init__(nid)
        self._model_func = model_func
        self._model_kwargs = model_kwargs or {}
        self._x_vars = x_vars
        self._y_vars = y_vars
        self._model = None
        self._col_mode = col_mode or "replace_all"
        dbg.dassert_in(self._col_mode, ["replace_all", "merge_all"])

    def fit(self, df_in: pd.DataFrame) -> Dict[str, pd.DataFrame]:
        SkLearnModel._validate_input_df(df_in)
        dbg.dassert(
            df_in[df_in.isna().any(axis=1)].index.empty,
            "NaNs detected at index `%s`",
            str(df_in[df_in.isna().any(axis=1)].head().index),
        )
        df = df_in.copy()
        idx = df.index
        x_vars, x_fit, y_vars, y_fit = self._to_sklearn_format(df)
        self._model = self._model_func(**self._model_kwargs)
        self._model = self._model.fit(x_fit, y_fit)
        y_hat = self._model.predict(x_fit)
        #
        x_fit, y_fit, y_hat = self._from_sklearn_format(
            idx, x_vars, x_fit, y_vars, y_fit, y_hat
        )
        # TODO(Paul): Summarize model perf or make configurable.
        # TODO(Paul): Consider separating model eval from fit/predict.
        info = collections.OrderedDict()
        info["model_x_vars"] = x_vars
        info["model_params"] = self._model.get_params()
        # Return targets and predictions.
        y_hat = y_hat.reindex(idx)
        df_out = self._apply_col_mode(
            df, y_hat, cols=y_vars, col_mode=self._col_mode
        )
        info["df_out_info"] = get_df_info_as_string(df_out)
        self._set_info("fit", info)
        return {"df_out": df_out}

    def predict(self, df_in: pd.DataFrame) -> Dict[str, pd.DataFrame]:
        SkLearnModel._validate_input_df(df_in)
        df = df_in.copy()
        idx = df.index
        x_vars, x_predict, y_vars, y_predict = self._to_sklearn_format(df)
        dbg.dassert_is_not(
            self._model, None, "Model not found! Check if `fit` has been run."
        )
        y_hat = self._model.predict(x_predict)
        x_predict, y_predict, y_hat = self._from_sklearn_format(
            idx, x_vars, x_predict, y_vars, y_predict, y_hat
        )
        info = collections.OrderedDict()
        info["model_params"] = self._model.get_params()
        info["model_perf"] = self._model_perf(x_predict, y_predict, y_hat)
        # Return predictions.
        y_hat = y_hat.reindex(idx)
        df_out = self._apply_col_mode(
            df, y_hat, cols=y_vars, col_mode=self._col_mode
        )
        info["df_out_info"] = get_df_info_as_string(df_out)
        self._set_info("predict", info)
        return {"df_out": df_out}

    def get_fit_state(self) -> Dict[str, Any]:
        fit_state = {"_model": self._model, "_info['fit']": self._info["fit"]}
        return fit_state

    def set_fit_state(self, fit_state: Dict[str, Any]):
        self._model = fit_state["_model"]
        self._info["fit"] = fit_state["_info['fit']"]

    @staticmethod
    def _validate_input_df(df: pd.DataFrame) -> None:
        """
        Assert if df violates constraints, otherwise return `None`.
        """
        dbg.dassert_isinstance(df, pd.DataFrame)
        dbg.dassert_no_duplicates(df.columns)

    # TODO(Paul): Add type hints.
    @staticmethod
    def _model_perf(
        x: pd.DataFrame, y: pd.DataFrame, y_hat: pd.DataFrame
    ) -> collections.OrderedDict:
        _ = x
        info = collections.OrderedDict()
        # info["hitrate"] = pip._compute_model_hitrate(self.model, x, y)
        pnl_rets = y.multiply(y_hat.rename(columns=lambda x: x.strip("_hat")))
        info["pnl_rets"] = pnl_rets
        info["sr"] = cstati.compute_sharpe_ratio(
            csigna.resample(pnl_rets, rule="1B").sum(), time_scaling=252
        )
        return info

    def _to_sklearn_format(
        self, df: pd.DataFrame
    ) -> Tuple[List[_COL_TYPE], np.array, List[_COL_TYPE], np.array]:
        x_vars = self._to_list(self._x_vars)
        y_vars = self._to_list(self._y_vars)
        x_vals, y_vals = cdataa.transform_to_sklearn_old(df, x_vars, y_vars)
        return x_vars, x_vals, y_vars, y_vals

    @staticmethod
    def _from_sklearn_format(
        idx: pd.Index,
        x_vars: List[_COL_TYPE],
        x_vals: np.array,
        y_vars: List[_COL_TYPE],
        y_vals: np.array,
        y_hat: np.array,
    ) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        x = cdataa.transform_from_sklearn(idx, x_vars, x_vals)
        y = cdataa.transform_from_sklearn(idx, y_vars, y_vals)
        y_h = cdataa.transform_from_sklearn(
            idx, [f"{y}_hat" for y in y_vars], y_hat
        )
        return x, y, y_h


# #############################################################################
# sklearn - unsupervised models
# #############################################################################


class UnsupervisedSkLearnModel(
    FitPredictNode, RegFreqMixin, ToListMixin, ColModeMixin
):
    """
    Fit and transform an unsupervised sklearn model.
    """

    # pylint: disable=too-many-ancestors

    def __init__(
        self,
        nid: str,
        model_func: Callable[..., Any],
        x_vars: Optional[_TO_LIST_MIXIN_TYPE] = None,
        model_kwargs: Optional[Any] = None,
        col_mode: Optional[str] = None,
        nan_mode: Optional[str] = None,
    ) -> None:
        """
        Specify the data and sklearn modeling parameters.

        :param nid: unique node id
        :param model_func: an sklearn model
        :param x_vars: indexed by knowledge datetimes
        :param model_kwargs: parameters to forward to the sklearn model
            (e.g., regularization constants)
        """
        super().__init__(nid)
        self._model_func = model_func
        self._model_kwargs = model_kwargs or {}
        self._x_vars = x_vars
        self._model = None
        self._col_mode = col_mode or "replace_all"
        dbg.dassert_in(self._col_mode, ["replace_all", "merge_all"])
        self._nan_mode = nan_mode or "raise"

    def fit(self, df_in: pd.DataFrame) -> Dict[str, pd.DataFrame]:
        return self._fit_predict_helper(df_in, fit=True)

    def predict(self, df_in: pd.DataFrame) -> Dict[str, pd.DataFrame]:
        return self._fit_predict_helper(df_in, fit=False)

    def get_fit_state(self) -> Dict[str, Any]:
        fit_state = {"_model": self._model, "_info['fit']": self._info["fit"]}
        return fit_state

    def set_fit_state(self, fit_state: Dict[str, Any]):
        self._model = fit_state["_model"]
        self._info["fit"] = fit_state["_info['fit']"]

    def _fit_predict_helper(
        self, df_in: pd.DataFrame, fit: bool = False
    ) -> Dict[str, pd.DataFrame]:
        """
        Factor out common flow for fit/predict.

        :param df_in: as in `fit`/`predict`
        :param fit: fits model iff `True`
        :return: transformed df_in
        """
        self._validate_input_df(df_in)
        df = df_in.copy()
        # Determine index where no x_vars are NaN.
        if self._x_vars is None:
            x_vars = df_in.columns.tolist()
        else:
            x_vars = self._to_list(self._x_vars)
        non_nan_idx = df[x_vars].dropna().index
        dbg.dassert(not non_nan_idx.empty)
        # Handle presence of NaNs according to `nan_mode`.
        self._handle_nans(df.index, non_nan_idx)
        # Prepare x_vars in sklearn format.
        x_fit = cdataa.transform_to_sklearn(df.loc[non_nan_idx], x_vars)
        if fit:
            # Define and fit model.
            self._model = self._model_func(**self._model_kwargs)
            self._model = self._model.fit(x_fit)
        # Generate insample transformations and put in dataflow dataframe format.
        x_transform = self._model.transform(x_fit)
        #
        num_cols = x_transform.shape[1]
        x_hat = cdataa.transform_from_sklearn(
            non_nan_idx, list(range(num_cols)), x_transform
        )
        info = collections.OrderedDict()
        info["model_x_vars"] = x_vars
        info["model_params"] = self._model.get_params()
        model_attribute_info = collections.OrderedDict()
        for k, v in vars(self._model).items():
            model_attribute_info[k] = v
        info["model_attributes"] = model_attribute_info
        # Return targets and predictions.
        df_out = x_hat.reindex(index=df_in.index)
        df_out = self._apply_col_mode(
            df, df_out, cols=x_vars, col_mode=self._col_mode
        )
        info["df_out_info"] = get_df_info_as_string(df_out)
        if fit:
            self._set_info("fit", info)
        else:
            self._set_info("predict", info)
        dbg.dassert_no_duplicates(df_out.columns)
        return {"df_out": df_out}

    def _handle_nans(
        self, idx: pd.DataFrame.index, non_nan_idx: pd.DataFrame.index
    ) -> None:
        if self._nan_mode == "raise":
            if idx.shape[0] != non_nan_idx.shape[0]:
                nan_idx = idx.difference(non_nan_idx)
                raise ValueError(f"NaNs detected at {nan_idx}")
        elif self._nan_mode == "drop":
            pass
        else:
            raise ValueError(f"Unrecognized nan_mode `{self._nan_mode}`")


class MultiindexUnsupervisedSkLearnModel(
    FitPredictNode, RegFreqMixin, ToListMixin, ColModeMixin
):
    """
    Fit and transform an unsupervised sklearn model.
    """

    # pylint: disable=too-many-ancestors

    def __init__(
        self,
        nid: str,
        in_col_group: Tuple[_COL_TYPE],
        out_col_group: Tuple[_COL_TYPE],
        model_func: Callable[..., Any],
        model_kwargs: Optional[Any] = None,
        nan_mode: Optional[str] = None,
    ) -> None:
        """
        Specify the data and sklearn modeling parameters.

        :param nid: unique node id
        :param model_func: an sklearn model
        :param model_kwargs: parameters to forward to the sklearn model
            (e.g., regularization constants)
        """
        super().__init__(nid)
        dbg.dassert_isinstance(in_col_group, tuple)
        dbg.dassert_isinstance(out_col_group, tuple)
        dbg.dassert_eq(
            len(in_col_group),
            len(out_col_group),
            msg="Column hierarchy depth must be preserved.",
        )
        self._in_col_group = in_col_group
        self._out_col_group = out_col_group
        self._model_func = model_func
        self._model_kwargs = model_kwargs or {}
        self._model = None
        self._nan_mode = nan_mode or "raise"

    def fit(self, df_in: pd.DataFrame) -> Dict[str, pd.DataFrame]:
        return self._fit_predict_helper(df_in, fit=True)

    def predict(self, df_in: pd.DataFrame) -> Dict[str, pd.DataFrame]:
        return self._fit_predict_helper(df_in, fit=False)

    def get_fit_state(self) -> Dict[str, Any]:
        fit_state = {"_model": self._model, "_info['fit']": self._info["fit"]}
        return fit_state

    def set_fit_state(self, fit_state: Dict[str, Any]):
        self._model = fit_state["_model"]
        self._info["fit"] = fit_state["_info['fit']"]

    def _fit_predict_helper(
        self, df_in: pd.DataFrame, fit: bool = False
    ) -> Tuple[pd.DataFrame, collections.OrderedDict]:
        """
        Factor out common flow for fit/predict.

        :param df_in: as in `fit`/`predict`
        :param fit: fits model iff `True`
        :return: transformed df_in
        """
        self._validate_input_df(df_in)
        # After indexing by `self._in_col_group`, we should have a flat column
        # index.
        dbg.dassert_eq(
            len(self._in_col_group),
            df_in.columns.nlevels - 1,
            "Dataframe multiindex column depth incompatible with config.",
            )
        # Do not allow overwriting existing columns.
        dbg.dassert_not_in(
            self._out_col_group,
            df_in.columns,
            "Desired column names already present in dataframe.",
        )
        df = df_in[self._in_col_group].copy()
        idx = df.index
        df = df_in.copy()
        # Determine index where no x_vars are NaN.
        x_vars = df.columns.tolist()
        non_nan_idx = df.dropna().index
        dbg.dassert(not non_nan_idx.empty)
        # Handle presence of NaNs according to `nan_mode`.
        self._handle_nans(df.index, non_nan_idx)
        # Prepare x_vars in sklearn format.
        x_fit = cdataa.transform_to_sklearn(df.loc[non_nan_idx], x_vars)
        if fit:
            # Define and fit model.
            self._model = self._model_func(**self._model_kwargs)
            self._model = self._model.fit(x_fit)
        # Generate insample transformations and put in dataflow dataframe format.
        x_transform = self._model.transform(x_fit)
        #
        num_cols = x_transform.shape[1]
        x_hat = cdataa.transform_from_sklearn(
            non_nan_idx, list(range(num_cols)), x_transform
        )
        info = collections.OrderedDict()
        info["model_x_vars"] = x_vars
        info["model_params"] = self._model.get_params()
        model_attribute_info = collections.OrderedDict()
        for k, v in vars(self._model).items():
            model_attribute_info[k] = v
        info["model_attributes"] = model_attribute_info
        # Return targets and predictions.
        df_out = x_hat.reindex(index=df_in.index)
        df_out = pd.concat([df_out], axis=1, keys=[self._out_col_group])
        df_out = df_out.merge(
            df_in,
            how="outer",
            left_index=True,
            right_index=True,
        )
        info["df_out_info"] = get_df_info_as_string(df_out)
        if fit:
            self._set_info("fit", info)
        else:
            self._set_info("predict", info)
        dbg.dassert_no_duplicates(df_out.columns)
        return {"df_out": df_out}

    def _handle_nans(
            self, idx: pd.DataFrame.index, non_nan_idx: pd.DataFrame.index
    ) -> None:
        if self._nan_mode == "raise":
            if idx.shape[0] != non_nan_idx.shape[0]:
                nan_idx = idx.difference(non_nan_idx)
                raise ValueError(f"NaNs detected at {nan_idx}")
        elif self._nan_mode == "drop":
            pass
        else:
            raise ValueError(f"Unrecognized nan_mode `{self._nan_mode}`")

class Residualizer(FitPredictNode, RegFreqMixin, ToListMixin):
    """
    Residualize using an sklearn model with `inverse_transform()`.
    """

    def __init__(
        self,
        nid: str,
        model_func: Callable[..., Any],
        x_vars: _TO_LIST_MIXIN_TYPE,
        model_kwargs: Optional[Any] = None,
        nan_mode: Optional[str] = None,
    ) -> None:
        """
        Specify the data and sklearn modeling parameters.

        :param nid: unique node id
        :param model_func: an sklearn model
        :param x_vars: indexed by knowledge datetimes
        :param model_kwargs: parameters to forward to the sklearn model
            (e.g., regularization constants)
        """
        super().__init__(nid)
        self._model_func = model_func
        self._model_kwargs = model_kwargs or {}
        self._x_vars = x_vars
        self._model = None
        self._nan_mode = nan_mode or "raise"

    def fit(self, df_in: pd.DataFrame) -> Dict[str, pd.DataFrame]:
        return self._fit_predict_helper(df_in, fit=True)

    def predict(self, df_in: pd.DataFrame) -> Dict[str, pd.DataFrame]:
        return self._fit_predict_helper(df_in, fit=False)

    def get_fit_state(self) -> Dict[str, Any]:
        fit_state = {"_model": self._model, "_info['fit']": self._info["fit"]}
        return fit_state

    def set_fit_state(self, fit_state: Dict[str, Any]):
        self._model = fit_state["_model"]
        self._info["fit"] = fit_state["_info['fit']"]

    def _fit_predict_helper(
        self, df_in: pd.DataFrame, fit: bool = False
    ) -> Dict[str, pd.DataFrame]:
        """
        Factor out common flow for fit/predict.

        :param df_in: as in `fit`/`predict`
        :param fit: fits model iff `True`
        :return: transformed df_in
        """
        self._validate_input_df(df_in)
        df = df_in.copy()
        # Determine index where no x_vars are NaN.
        x_vars = self._to_list(self._x_vars)
        non_nan_idx = df[x_vars].dropna().index
        dbg.dassert(not non_nan_idx.empty)
        # Handle presence of NaNs according to `nan_mode`.
        self._handle_nans(df.index, non_nan_idx)
        # Prepare x_vars in sklearn format.
        x_fit = cdataa.transform_to_sklearn(df.loc[non_nan_idx], x_vars)
        if fit:
            # Define and fit model.
            self._model = self._model_func(**self._model_kwargs)
            self._model = self._model.fit(x_fit)
        # Generate insample transformations and put in dataflow dataframe format.
        x_transform = self._model.transform(x_fit)
        x_hat = self._model.inverse_transform(x_transform)
        #
        x_residual = cdataa.transform_from_sklearn(
            non_nan_idx, x_vars, x_fit - x_hat
        )
        info = collections.OrderedDict()
        info["model_x_vars"] = x_vars
        info["model_params"] = self._model.get_params()
        model_attribute_info = collections.OrderedDict()
        for k, v in vars(self._model).items():
            model_attribute_info[k] = v
        info["model_attributes"] = model_attribute_info
        df_out = x_residual.reindex(index=df_in.index)
        info["df_out_info"] = get_df_info_as_string(df_out)
        if fit:
            self._set_info("fit", info)
        else:
            self._set_info("predict", info)
        # Return targets and predictions.
        return {"df_out": df_out}

    def _handle_nans(
        self, idx: pd.DataFrame.index, non_nan_idx: pd.DataFrame.index
    ) -> None:
        if self._nan_mode == "raise":
            if idx.shape[0] != non_nan_idx.shape[0]:
                nan_idx = idx.difference(non_nan_idx)
                raise ValueError(f"NaNs detected at {nan_idx}")
        elif self._nan_mode == "drop":
            pass
        else:
            raise ValueError(f"Unrecognized nan_mode `{self._nan_mode}`")


class SkLearnInverseTransformer(
    FitPredictNode, RegFreqMixin, ToListMixin, ColModeMixin
):
    """
    Inverse transform cols using an unsupervised sklearn model.
    """

    # pylint: disable=too-many-ancestors

    def __init__(
        self,
        nid: str,
        model_func: Callable[..., Any],
        x_vars: _TO_LIST_MIXIN_TYPE,
        trans_x_vars: _TO_LIST_MIXIN_TYPE,
        model_kwargs: Optional[Any] = None,
        col_mode: Optional[str] = None,
        nan_mode: Optional[str] = None,
    ) -> None:
        """
        Specify the data and sklearn modeling parameters.

        :param nid: unique node id
        :param model_func: an unsupervised sklearn model with an
            `inverse_transform()` method
        :param x_vars: indexed by knowledge datetimes; the unsupervised model
            is learned on these cols
        :param trans_x_vars: the cols to apply the learned inverse transform to
        :param model_kwargs: parameters to forward to the sklearn model
            (e.g., regularization constants)
        """
        super().__init__(nid)
        self._model_func = model_func
        self._model_kwargs = model_kwargs or {}
        self._x_vars = self._to_list(x_vars)
        self._trans_x_vars = self._to_list(trans_x_vars)
        dbg.dassert_not_intersection(self._x_vars, self._trans_x_vars)
        self._model = None
        self._col_mode = col_mode or "replace_all"
        dbg.dassert_in(self._col_mode, ["replace_all", "merge_all"])
        self._nan_mode = nan_mode or "raise"

    def fit(self, df_in: pd.DataFrame) -> Dict[str, pd.DataFrame]:
        return self._fit_predict_helper(df_in, fit=True)

    def predict(self, df_in: pd.DataFrame) -> Dict[str, pd.DataFrame]:
        return self._fit_predict_helper(df_in, fit=False)

    def get_fit_state(self) -> Dict[str, Any]:
        fit_state = {"_model": self._model, "_info['fit']": self._info["fit"]}
        return fit_state

    def set_fit_state(self, fit_state: Dict[str, Any]):
        self._model = fit_state["_model"]
        self._info["fit"] = fit_state["_info['fit']"]

    def _fit_predict_helper(
        self, df_in: pd.DataFrame, fit: bool = False
    ) -> Dict[str, pd.DataFrame]:
        """
        Factor out common flow for fit/predict.

        :param df_in: as in `fit`/`predict`
        :param fit: fits model iff `True`
        :return: transformed df_in
        """
        self._validate_input_df(df_in)
        df = df_in.copy()
        # Determine index where no x_vars are NaN.
        x_vars = self._to_list(self._x_vars)
        non_nan_idx = df[x_vars].dropna().index
        dbg.dassert(not non_nan_idx.empty)
        # Handle presence of NaNs according to `nan_mode`.
        self._handle_nans(df.index, non_nan_idx)
        # Prepare x_vars in sklearn format.
        x_fit = cdataa.transform_to_sklearn(df.loc[non_nan_idx], x_vars)
        if fit:
            # Define and fit model.
            self._model = self._model_func(**self._model_kwargs)
            self._model = self._model.fit(x_fit)
        # Add info on unsupervised model.
        info = collections.OrderedDict()
        info["model_x_vars"] = x_vars
        info["model_params"] = self._model.get_params()
        model_attribute_info = collections.OrderedDict()
        for k, v in vars(self._model).items():
            model_attribute_info[k] = v
        info["model_attributes"] = model_attribute_info
        # Determine index where no trans_x_vars are NaN.
        trans_x_vars = self._to_list(self._trans_x_vars)
        trans_non_nan_idx = df[trans_x_vars].dropna().index
        dbg.dassert(not trans_non_nan_idx.empty)
        # Handle presence of NaNs according to `nan_mode`.
        self._handle_nans(df.index, trans_non_nan_idx)
        # Prepare trans_x_vars in sklearn format.
        trans_x_fit = cdataa.transform_to_sklearn(
            df.loc[non_nan_idx], trans_x_vars
        )
        trans_x_inv_trans = self._model.inverse_transform(trans_x_fit)
        trans_x_inv_trans = cdataa.transform_from_sklearn(
            trans_non_nan_idx, x_vars, trans_x_inv_trans
        )
        #
        df_out = trans_x_inv_trans.reindex(index=df_in.index)
        df_out = self._apply_col_mode(
            df, df_out, cols=trans_x_vars, col_mode=self._col_mode
        )
        info["df_out_info"] = get_df_info_as_string(df_out)
        if fit:
            self._set_info("fit", info)
        else:
            self._set_info("predict", info)
        dbg.dassert_no_duplicates(df_out.columns)
        return {"df_out": df_out}

    def _handle_nans(
        self, idx: pd.DataFrame.index, non_nan_idx: pd.DataFrame.index
    ) -> None:
        if self._nan_mode == "raise":
            if idx.shape[0] != non_nan_idx.shape[0]:
                nan_idx = idx.difference(non_nan_idx)
                raise ValueError(f"NaNs detected at {nan_idx}")
        elif self._nan_mode == "drop":
            pass
        else:
            raise ValueError(f"Unrecognized nan_mode `{self._nan_mode}`")


# #############################################################################
# Volatility modeling
# #############################################################################


class SmaModel(FitPredictNode, RegFreqMixin, ColModeMixin, ToListMixin):
    """
    Fit and predict a smooth moving average model.
    """

    def __init__(
        self,
        nid: str,
        col: _TO_LIST_MIXIN_TYPE,
        steps_ahead: int,
        tau: Optional[float] = None,
        min_tau_periods: Optional[float] = 2,
        col_mode: Optional[str] = None,
        nan_mode: Optional[str] = None,
    ) -> None:
        """
        Specify the data and sma modeling parameters.

        :param nid: unique node id
        :param col: name of column to model
        :param steps_ahead: as in ContinuousSkLearnModel
        :param tau: as in `csigna.compute_smooth_moving_average`. If `None`,
            learn this parameter. Will be re-learned on each `fit` call.
        :param min_tau_periods: similar to `min_periods` as in
            `csigna.compute_smooth_moving_average`, but expressed in units of
            tau
        :param col_mode: `merge_all` or `replace_all`, as in
            ColumnTransformer()
        :param nan_mode: as in ContinuousSkLearnModel
        """
        super().__init__(nid)
        self._col = self._to_list(col)
        dbg.dassert_eq(len(self._col), 1)
        self._steps_ahead = steps_ahead
        dbg.dassert_lte(
            0, self._steps_ahead, "Non-causal prediction attempted! Aborting..."
        )
        if nan_mode is None:
            self._nan_mode = "raise"
        else:
            self._nan_mode = nan_mode
        self._col_mode = col_mode or "replace_all"
        dbg.dassert_in(self._col_mode, ["replace_all", "merge_all"])
        # Smooth moving average model parameters to learn.
        self._must_learn_tau = tau is None
        self._tau = tau
        self._min_tau_periods = min_tau_periods or 0
        self._min_depth = 1
        self._max_depth = 1
        self._metric = sklear.metrics.mean_absolute_error

    def fit(self, df_in: pd.DataFrame) -> Dict[str, pd.DataFrame]:
        self._validate_input_df(df_in)
        df = df_in.copy()
        # Obtain index slice for which forward targets exist.
        dbg.dassert_lt(self._steps_ahead, df.index.size)
        idx = df.index[: -self._steps_ahead]
        # Determine index where no x_vars are NaN.
        non_nan_idx_x = df.loc[idx][self._col].dropna().index
        # Determine index where target is not NaN.
        fwd_y_df = self._get_fwd_y_df(df).loc[idx].dropna()
        non_nan_idx_fwd_y = fwd_y_df.dropna().index
        # Intersect non-NaN indices.
        non_nan_idx = non_nan_idx_x.intersection(non_nan_idx_fwd_y)
        dbg.dassert(not non_nan_idx.empty)
        fwd_y_df = fwd_y_df.loc[non_nan_idx]
        # Handle presence of NaNs according to `nan_mode`.
        self._handle_nans(idx, non_nan_idx)
        # Prepare x_vars in sklearn format.
        x_fit = cdataa.transform_to_sklearn(df.loc[non_nan_idx], self._col)
        # Prepare forward y_vars in sklearn format.
        fwd_y_fit = cdataa.transform_to_sklearn(
            fwd_y_df, fwd_y_df.columns.tolist()
        )
        # Define and fit model.
        if self._must_learn_tau:
            self._tau = self._learn_tau(x_fit, fwd_y_fit)
        min_periods = self._get_min_periods(self._tau)
        _LOG.debug("tau=", self._tau)
        info = collections.OrderedDict()
        info["tau"] = self._tau
        info["min_periods"] = min_periods
        # Generate insample predictions and put in dataflow dataframe format.
        fwd_y_hat = self._predict(x_fit)
        fwd_y_hat_vars = [f"{y}_hat" for y in fwd_y_df.columns]
        fwd_y_hat = cdataa.transform_from_sklearn(
            non_nan_idx, fwd_y_hat_vars, fwd_y_hat
        )
        # Return targets and predictions.
        df_out = fwd_y_df.reindex(idx).merge(
            fwd_y_hat.reindex(idx), left_index=True, right_index=True
        )
        dbg.dassert_no_duplicates(df_out.columns)
        df_out = self._apply_col_mode(
            df, df_out, cols=self._col, col_mode=self._col_mode
        )
        info["df_out_info"] = get_df_info_as_string(df_out)
        self._set_info("fit", info)
        return {"df_out": df_out}

    def predict(self, df_in: pd.DataFrame) -> Dict[str, pd.DataFrame]:
        self._validate_input_df(df_in)
        df = df_in.copy()
        idx = df.index
        # Restrict to times where col has no NaNs.
        non_nan_idx = df.loc[idx][self._col].dropna().index
        # Handle presence of NaNs according to `nan_mode`.
        self._handle_nans(idx, non_nan_idx)
        # Transform x_vars to sklearn format.
        x_predict = cdataa.transform_to_sklearn(df.loc[non_nan_idx], self._col)
        # Use trained model to generate predictions.
        dbg.dassert_is_not(
            self._tau,
            None,
            "Parameter tau not found! Check if `fit` has been run.",
        )
        fwd_y_hat = self._predict(x_predict)
        # Put predictions in dataflow dataframe format.
        fwd_y_df = self._get_fwd_y_df(df).loc[non_nan_idx]
        fwd_y_hat_vars = [f"{y}_hat" for y in fwd_y_df.columns]
        fwd_y_hat = cdataa.transform_from_sklearn(
            non_nan_idx, fwd_y_hat_vars, fwd_y_hat
        )
        # Return targets and predictions.
        df_out = fwd_y_df.reindex(idx).merge(
            fwd_y_hat.reindex(idx), left_index=True, right_index=True
        )
        dbg.dassert_no_duplicates(df_out.columns)
        info = collections.OrderedDict()
        df_out = self._apply_col_mode(
            df, df_out, cols=self._col, col_mode=self._col_mode
        )
        info["df_out_info"] = get_df_info_as_string(df_out)
        self._set_info("predict", info)
        return {"df_out": df_out}

    def get_fit_state(self) -> Dict[str, Any]:
        fit_state = {"_tau": self._tau, "_info['fit']": self._info["fit"]}
        return fit_state

    def set_fit_state(self, fit_state: Dict[str, Any]):
        self._tau = fit_state["_tau"]
        self._info["fit"] = fit_state["_info['fit']"]

    def _get_fwd_y_df(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Return dataframe of `steps_ahead` forward y values.
        """
        mapper = lambda y: str(y) + "_%i" % self._steps_ahead
        # TODO(Paul): Ensure that `fwd_y_vars` and `y_vars` do not overlap.
        fwd_y_df = df[self._col].shift(-self._steps_ahead).rename(columns=mapper)
        return fwd_y_df

    def _handle_nans(
        self, idx: pd.DataFrame.index, non_nan_idx: pd.DataFrame.index
    ) -> None:
        if self._nan_mode == "raise":
            if idx.shape[0] != non_nan_idx.shape[0]:
                nan_idx = idx.difference(non_nan_idx)
                raise ValueError(f"NaNs detected at {nan_idx}")
        elif self._nan_mode == "drop":
            pass
        elif self._nan_mode == "leave_unchanged":
            pass
        else:
            raise ValueError(f"Unrecognized nan_mode `{self._nan_mode}`")

    def _learn_tau(self, x: np.array, y: np.array) -> float:
        def score(tau: float) -> float:
            x_srs = pd.DataFrame(x.flatten())
            sma = csigna.compute_smooth_moving_average(
                x_srs,
                tau=tau,
                min_periods=0,
                min_depth=self._min_depth,
                max_depth=self._max_depth,
            )
            min_periods = self._get_min_periods(tau)
            return self._metric(sma[min_periods:], y[min_periods:])

        tau_lb, tau_ub = 1, 1000
        # Satisfy 2 * tau_ub * min_tau_periods = len(x).
        # This ensures that no more than half of the `fit` series is burned.
        if self._min_tau_periods > 0:
            tau_ub = int(len(x) / (2 * self._min_tau_periods))
        opt_results = sp.optimize.minimize_scalar(
            score, method="bounded", bounds=[tau_lb, tau_ub]
        )
        return opt_results.x

    def _get_min_periods(self, tau: float) -> int:
        """
        Return burn-in period.

        Multiplies `tau` by `min_tau_periods` and converts to an integer.

        :param tau: kernel tau (approximately equal to com)
        :return: minimum number of periods required to generate a prediction
        """
        return int(np.rint(self._min_tau_periods * tau))

    def _predict(self, x: np.array) -> np.array:
        x_srs = pd.DataFrame(x.flatten())
        # TODO(*): Make `min_periods` configurable.
        min_periods = int(np.rint(self._min_tau_periods * self._tau))
        _LOG.debug("min_periods=%f", min_periods)
        x_sma = csigna.compute_smooth_moving_average(
            x_srs,
            tau=self._tau,
            min_periods=min_periods,
            min_depth=self._min_depth,
            max_depth=self._max_depth,
        )
        return x_sma.values

    # TODO(Paul): Consider omitting this (and relying on downstream
    #     processing to e.g., adjust for number of hypotheses tested).
    @staticmethod
    def _model_perf(
        y: pd.DataFrame, y_hat: pd.DataFrame
    ) -> collections.OrderedDict:
        info = collections.OrderedDict()
        # info["hitrate"] = pip._compute_model_hitrate(self.model, x, y)
        pnl_rets = y.multiply(
            y_hat.rename(columns=lambda x: x.replace("_hat", ""))
        )
        info["pnl_rets"] = pnl_rets
        info["sr"] = cstati.compute_annualized_sharpe_ratio(
            csigna.resample(pnl_rets, rule="1B").sum()
        )
        return info


class VolatilityModel(FitPredictNode, RegFreqMixin, ColModeMixin, ToListMixin):
    """
    Fit and predict a smooth moving average volatility model.

    Wraps SmaModel internally, handling calculation of volatility from
    returns and column appends.
    """

    def __init__(
        self,
        nid: str,
        steps_ahead: int,
        cols: Optional[_TO_LIST_MIXIN_TYPE] = None,
        p_moment: float = 2,
        tau: Optional[float] = None,
        col_rename_func: Callable[[Any], Any] = lambda x: f"{x}_zscored",
        col_mode: Optional[str] = None,
        nan_mode: Optional[str] = None,
    ) -> None:
        """
        Specify the data and sma modeling parameters.

        :param nid: unique node id
        :param cols: name of columns to model
        :param steps_ahead: as in ContinuousSkLearnModel
        :param p_moment: exponent to apply to the absolute value of returns
        :param tau: as in `csigna.compute_smooth_moving_average`. If `None`,
            learn this parameter
        :param col_rename_func: renaming function for z-scored column
        :param col_mode:
            - If "merge_all", merge all columns from input dataframe and
                transformed columns
            - If "replace_selected", merge unselected columns from input dataframe
                and transformed selected columns
            - If "replace_all", leave only transformed selected columns
        :param nan_mode: as in ContinuousSkLearnModel
        """
        super().__init__(nid)
        self._cols = cols
        self._steps_ahead = steps_ahead
        dbg.dassert_lte(1, p_moment)
        self._p_moment = p_moment
        #
        self._tau = tau
        self._col_rename_func = col_rename_func
        self._col_mode = col_mode or "merge_all"
        self._nan_mode = nan_mode
        #
        self._fit_cols: List[_COL_TYPE] = []
        self._vol_cols: Dict[_COL_TYPE, str] = {}
        self._fwd_vol_cols: Dict[_COL_TYPE, str] = {}
        self._fwd_vol_cols_hat: Dict[_COL_TYPE, str] = {}
        self._taus: Dict[_COL_TYPE, Optional[float]] = {}

    def fit(self, df_in: pd.DataFrame) -> Dict[str, pd.DataFrame]:
        self._validate_input_df(df_in)
        self._fit_cols = self._to_list(self._cols or df_in.columns.tolist())
        self._vol_cols = {col: str(col) + "_vol" for col in self._fit_cols}
        self._fwd_vol_cols = {
            col: self._vol_cols[col] + f"_{self._steps_ahead}"
            for col in self._fit_cols
        }
        self._fwd_vol_cols_hat = {
            col: self._fwd_vol_cols[col] + "_hat" for col in self._fit_cols
        }
        self._check_cols(df_in.columns.tolist())
        info = collections.OrderedDict()
        dfs = []
        for col in self._fit_cols:
            dbg.dassert_not_in(self._vol_cols[col], self._fit_cols)
            config = self._get_config(col=col, tau=self._tau)
            dag = self._get_dag(df_in[[col]], config)
            df_out = dag.run_leq_node("demodulate_using_vol_pred", "fit")[
                "df_out"
            ]
            info[col] = extract_info(dag, ["fit"])
            if self._tau is None:
                self._taus[col] = info[col]["compute_smooth_moving_average"][
                    "fit"
                ]["tau"]
            else:
                self._taus[col] = self._tau
            dfs.append(df_out)
        df_out = pd.concat(dfs, axis=1)
        df_out = self._apply_col_mode(
            df_in.drop(df_out.columns.intersection(df_in.columns), 1),
            df_out,
            cols=self._fit_cols,
            col_mode=self._col_mode,
        )
        df_out = df_out.reindex(df_in.index)
        self._set_info("fit", info)
        return {"df_out": df_out}

    def predict(self, df_in: pd.DataFrame) -> Dict[str, pd.DataFrame]:
        self._validate_input_df(df_in)
        self._check_cols(df_in.columns.tolist())
        info = collections.OrderedDict()
        dfs = []
        for col in self._fit_cols:
            dbg.dassert_not_in(self._vol_cols[col], df_in.columns)
            tau = self._taus[col]
            dbg.dassert(tau)
            config = self._get_config(col=col, tau=tau)
            dag = self._get_dag(df_in[[col]], config)
            df_out = dag.run_leq_node("demodulate_using_vol_pred", "predict")[
                "df_out"
            ]
            info[col] = extract_info(dag, ["predict"])
            dfs.append(df_out)
        df_out = pd.concat(dfs, axis=1)
        df_out = self._apply_col_mode(
            df_in.drop(df_out.columns.intersection(df_in.columns), 1),
            df_out,
            cols=self._fit_cols,
            col_mode=self._col_mode,
        )
        df_out = df_out.reindex(df_in.index)
        self._set_info("predict", info)
        return {"df_out": df_out}

    @property
    def taus(self) -> Dict[_COL_TYPE, Any]:
        return self._taus

    def get_fit_state(self) -> Dict[str, Any]:
        fit_state = {
            "_fit_cols": self._fit_cols,
            "_vol_cols": self._vol_cols,
            "_fwd_vol_cols": self._fwd_vol_cols,
            "_fwd_vol_cols_hat": self._fwd_vol_cols_hat,
            "_taus": self._taus,
            "_info['fit']": self._info["fit"],
        }
        return fit_state

    def set_fit_state(self, fit_state: Dict[str, Any]):
        self._fit_cols = fit_state["_fit_cols"]
        self._vol_cols = fit_state["_vol_cols"]
        self._fwd_vol_cols = fit_state["_fwd_vol_cols"]
        self._fwd_vol_cols_hat = fit_state["_fwd_vol_cols_hat"]
        self._taus = fit_state["_taus"]
        self._info["fit"] = fit_state["_info['fit']"]

    def _check_cols(self, cols: List[_COL_TYPE]):
        dbg.dassert_not_intersection(cols, self._vol_cols.values())
        dbg.dassert_not_intersection(cols, self._fwd_vol_cols.values())
        dbg.dassert_not_intersection(cols, self._fwd_vol_cols_hat.values())

    def _get_config(
        self, col: _COL_TYPE, tau: Optional[float] = None
    ) -> cconfi.Config:
        """
        Generate a DAG config.

        :param col: column whose volatility is to be modeled
        :param tau: tau for SMA; if `None`, then to be learned
        :return: a complete config to be used with `_get_dag()`
        """
        config = ccbuild.get_config_from_nested_dict(
            {
                "calculate_vol_pth_power": {
                    "cols": [col],
                    "col_rename_func": lambda x: f"{x}_vol",
                    "col_mode": "merge_all",
                },
                "compute_smooth_moving_average": {
                    "col": [self._vol_cols[col]],
                    "steps_ahead": self._steps_ahead,
                    "tau": tau,
                    "col_mode": "merge_all",
                    "nan_mode": self._nan_mode,
                },
                "calculate_vol_pth_root": {
                    "cols": [
                        self._vol_cols[col],
                        self._fwd_vol_cols[col],
                        self._fwd_vol_cols_hat[col],
                    ],
                    "col_mode": "replace_selected",
                },
                "demodulate_using_vol_pred": {
                    "signal_cols": [col],
                    "volatility_col": self._fwd_vol_cols_hat[col],
                    "signal_steps_ahead": 0,
                    "volatility_steps_ahead": self._steps_ahead,
                    "col_rename_func": self._col_rename_func,
                    "col_mode": "replace_selected",
                    "nan_mode": self._nan_mode,
                },
            }
        )
        return config

    def _get_dag(self, df_in: pd.DataFrame, config: cconfi.Config) -> DAG:
        """
        Build a DAG from data and config.

        :param df_in: data over which to run DAG
        :param config: config for configuring DAG nodes
        :return: ready-to-run DAG
        """
        dag = DAG(mode="strict")
        _LOG.debug("%s", config)
        # Load `df_in`.
        nid = "load_data"
        node = ReadDataFromDf(nid, df_in)
        tail_nid = self._append(dag, None, node)
        # Raise volatility columns to pth power.
        nid = "calculate_vol_pth_power"
        node = ColumnTransformer(
            nid,
            transformer_func=lambda x: np.abs(x) ** self._p_moment,
            **config[nid].to_dict(),
        )
        tail_nid = self._append(dag, tail_nid, node)
        # Predict pth power of volatility using smooth moving average.
        nid = "compute_smooth_moving_average"
        node = SmaModel(nid, **config[nid].to_dict())
        tail_nid = self._append(dag, tail_nid, node)
        # Calculate the pth root of volatility columns.
        nid = "calculate_vol_pth_root"
        node = ColumnTransformer(
            nid,
            transformer_func=lambda x: np.abs(x) ** (1.0 / self._p_moment),
            **config[nid].to_dict(),
        )
        tail_nid = self._append(dag, tail_nid, node)
        # Divide returns by volatilty prediction.
        nid = "demodulate_using_vol_pred"
        node = VolatilityModulator(
            nid, mode="demodulate", **config[nid].to_dict()
        )
        self._append(dag, tail_nid, node)
        return dag

    @staticmethod
    def _append(dag: DAG, tail_nid: Optional[str], node: Node) -> str:
        dag.add_node(node)
        if tail_nid is not None:
            dag.connect(tail_nid, node.nid)
        return node.nid


class MultiindexVolatilityModel(FitPredictNode, RegFreqMixin, ToListMixin):
    """
    Fit and predict a smooth moving average volatility model.

    Wraps SmaModel internally, handling calculation of volatility from
    returns and column appends.

    TODO(*): There is a lot of code shared with `MultiindexSeriesTransformer`.
        Can anything be shared?
    TODO(*): We hit
        ```
        PerformanceWarning: indexing past lexsort depth may impact performance.
        ```
    TODO(*): Add tests.
    TODO(*): Ensure new column names do not collide with existing ones.
    """

    def __init__(
        self,
        nid: str,
        in_col_group: Tuple[_COL_TYPE],
        steps_ahead: int,
        p_moment: float = 2,
        out_col_prefix: Optional[str] = None,
        tau: Optional[float] = None,
        nan_mode: Optional[str] = None,
    ) -> None:
        """
        Specify the data and sma modeling parameters.

        :param nid: unique node id
        :param steps_ahead: as in ContinuousSkLearnModel
        :param p_moment: exponent to apply to the absolute value of returns
        :param tau: as in `csigna.compute_smooth_moving_average`. If `None`,
            learn this parameter
        :param nan_mode: as in ContinuousSkLearnModel
        """
        super().__init__(nid)
        dbg.dassert_isinstance(in_col_group, tuple)
        self._in_col_group = in_col_group
        self._out_col_prefix = out_col_prefix or ""
        #
        self._steps_ahead = steps_ahead
        dbg.dassert_lte(1, p_moment)
        self._p_moment = p_moment
        #
        self._tau = tau
        self._nan_mode = nan_mode
        #
        self._leaf_cols: List[_COL_TYPE] = []
        self._taus: Dict[_COL_TYPE, Optional[float]] = {}

    def fit(self, df_in: pd.DataFrame) -> Dict[str, pd.DataFrame]:
        self._validate_input_df(df_in)
        # After indexing by `self._in_col_group`, we should have a flat column
        # index.
        dbg.dassert_eq(
            len(self._in_col_group),
            df_in.columns.nlevels - 1,
            "Dataframe multiindex column depth incompatible with config.",
        )
        dbg.dassert_eq(
            df_in.columns.nlevels,
            2,
            "Only multiindices of depth=2 currently supported.",
        )
        df = df_in[self._in_col_group].copy()
        self._leaf_cols = df.columns.tolist()
        idx = df.index
        info = collections.OrderedDict()
        dfs = []
        for col in self._leaf_cols:
            config = self._get_config(col=col, tau=self._tau)
            dag = self._get_dag(df[[col]], config)
            df_out = dag.run_leq_node("drop_col", "fit")["df_out"]
            info[col] = extract_info(dag, ["fit"])
            if self._tau is None:
                self._taus[col] = info[col]["compute_smooth_moving_average"][
                    "fit"
                ]["tau"]
            else:
                self._taus[col] = self._tau
            df_out = pd.concat([df_out], axis=1, keys=[col])
            dfs.append(df_out)
        df_out = pd.concat(dfs, axis=1)
        df_out = df_out.reindex(idx)
        df_out = df_out.swaplevel(i=0, j=1, axis=1)
        df_out.sort_index(axis=1, level=0, inplace=True)
        df_out = df_out.merge(
            df_in,
            how="outer",
            left_index=True,
            right_index=True,
        )
        # TODO(*): merge with input.
        self._set_info("fit", info)
        return {"df_out": df_out}

    def predict(self, df_in: pd.DataFrame) -> Dict[str, pd.DataFrame]:
        self._validate_input_df(df_in)
        dbg.dassert_eq(
            len(self._in_col_group),
            df_in.columns.nlevels - 1,
            "Dataframe multiindex column depth incompatible with config.",
        )
        dbg.dassert_eq(
            df_in.columns.nlevels,
            2,
            "Only multiindices of depth=2 currently supported.",
        )
        df = df_in[self._in_col_group].copy()
        self._leaf_cols = df.columns.tolist()
        idx = df.index
        info = collections.OrderedDict()
        dfs = []
        for col in self._leaf_cols:
            dbg.dassert_in(
                col,
                self._taus.keys(),
                msg=f"No `tau` for `col={col}` found. Check that model has been fit.",
            )
            tau = self._taus[col]
            dbg.dassert(tau)
            config = self._get_config(col=col, tau=tau)
            dag = self._get_dag(df[[col]], config)
            df_out = dag.run_leq_node("drop_col", "predict")["df_out"]
            info[col] = extract_info(dag, ["predict"])
            df_out = pd.concat([df_out], axis=1, keys=[col])
            dfs.append(df_out)
        df_out = pd.concat(dfs, axis=1)
        df_out = df_out.reindex(idx)
        df_out = df_out.swaplevel(i=0, j=1, axis=1)
        df_out.sort_index(axis=1, level=0, inplace=True)
        df_out = df_out.merge(
            df_in,
            how="outer",
            left_index=True,
            right_index=True,
        )
        self._set_info("predict", info)
        return {"df_out": df_out}

    @property
    def taus(self) -> Dict[_COL_TYPE, Any]:
        return self._taus

    def get_fit_state(self) -> Dict[str, Any]:
        fit_state = {
            "_leaf_cols": self._leaf_cols,
            "_taus": self._taus,
            "_info['fit']": self._info["fit"],
        }
        return fit_state

    def set_fit_state(self, fit_state: Dict[str, Any]):
        self._leaf_cols = fit_state["_leaf_cols"]
        self._taus = fit_state["_taus"]
        self._info["fit"] = fit_state["_info['fit']"]

    def _get_config(
        self, col: _COL_TYPE, tau: Optional[float] = None
    ) -> cconfi.Config:
        """
        Generate a DAG config.

        :param col: column whose volatility is to be modeled
        :param tau: tau for SMA; if `None`, then to be learned
        :return: a complete config to be used with `_get_dag()`
        """
        config = ccbuild.get_config_from_nested_dict(
            {
                "calculate_vol_pth_power": {
                    "cols": [col],
                    "col_rename_func": lambda x: self._out_col_prefix + "vol",
                    "col_mode": "merge_all",
                },
                "compute_smooth_moving_average": {
                    "col": [self._out_col_prefix + "vol"],
                    "steps_ahead": self._steps_ahead,
                    "tau": tau,
                    "col_mode": "merge_all",
                    "nan_mode": self._nan_mode,
                },
                "calculate_vol_pth_root": {
                    "cols": [
                        self._out_col_prefix + "vol",
                        self._out_col_prefix + "vol_" + str(self._steps_ahead),
                        self._out_col_prefix
                        + "vol_"
                        + str(self._steps_ahead)
                        + "_hat",
                    ],
                    "col_mode": "replace_selected",
                },
                "demodulate_using_vol_pred": {
                    "signal_cols": [col],
                    "volatility_col": self._out_col_prefix
                    + "vol_"
                    + str(self._steps_ahead)
                    + "_hat",
                    "signal_steps_ahead": 0,
                    "volatility_steps_ahead": self._steps_ahead,
                    "col_mode": "replace_selected",
                    "nan_mode": self._nan_mode,
                },
                "drop_col": {
                    "method": "drop",
                    "method_kwargs": {
                        "columns": col,
                        "axis": 1,
                    },
                },
            }
        )
        return config

    def _get_dag(self, df_in: pd.DataFrame, config: cconfi.Config) -> DAG:
        """
        Build a DAG from data and config.

        :param df_in: data over which to run DAG
        :param config: config for configuring DAG nodes
        :return: ready-to-run DAG
        """
        dag = DAG(mode="strict")
        _LOG.debug("%s", config)
        # Load `df_in`.
        nid = "load_data"
        node = ReadDataFromDf(nid, df_in)
        tail_nid = self._append(dag, None, node)
        # Raise volatility columns to pth power.
        nid = "calculate_vol_pth_power"
        node = ColumnTransformer(
            nid,
            transformer_func=lambda x: np.abs(x) ** self._p_moment,
            **config[nid].to_dict(),
        )
        tail_nid = self._append(dag, tail_nid, node)
        # Predict pth power of volatility using smooth moving average.
        nid = "compute_smooth_moving_average"
        node = SmaModel(nid, **config[nid].to_dict())
        tail_nid = self._append(dag, tail_nid, node)
        # Calculate the pth root of volatility columns.
        nid = "calculate_vol_pth_root"
        node = ColumnTransformer(
            nid,
            transformer_func=lambda x: np.abs(x) ** (1.0 / self._p_moment),
            **config[nid].to_dict(),
        )
        tail_nid = self._append(dag, tail_nid, node)
        # Divide returns by volatilty prediction.
        nid = "demodulate_using_vol_pred"
        node = VolatilityModulator(
            nid, mode="demodulate", **config[nid].to_dict()
        )
        tail_nid = self._append(dag, tail_nid, node)
        # Drop input column.
        nid = "drop_col"
        node = DataframeMethodRunner(nid, **config[nid].to_dict())
        self._append(dag, tail_nid, node)
        return dag

    @staticmethod
    def _append(dag: DAG, tail_nid: Optional[str], node: Node) -> str:
        dag.add_node(node)
        if tail_nid is not None:
            dag.connect(tail_nid, node.nid)
        return node.nid


class VolatilityModulator(FitPredictNode, ColModeMixin, ToListMixin):
    """
    Modulate or demodulate signal by volatility.

    Processing steps:
      - shift volatility to align it with signal
      - multiply/divide signal by volatility

    Usage examples:
      - Z-scoring
        - to obtain volatility prediction, pass in returns into `SmaModel` with
          a `steps_ahead` parameter
        - to z-score, pass in signal, volatility prediction, `signal_steps_ahead=0`,
          `volatility_steps_ahead=steps_ahead`, `mode='demodulate'`
      - Undoing z-scoring
        - Let's say we have
          - forward volatility prediction `n` steps ahead
          - prediction of forward z-scored returns `m` steps ahead. Z-scoring
            for the target has been done using the volatility prediction above
        - To undo z-scoring, we need to pass in the prediction of forward
          z-scored returns, forward volatility prediction, `signal_steps_ahead=n`,
          `volatility_steps_ahead=m`, `mode='modulate'`
    """

    def __init__(
        self,
        nid: str,
        signal_cols: _TO_LIST_MIXIN_TYPE,
        volatility_col: _COL_TYPE,
        signal_steps_ahead: int,
        volatility_steps_ahead: int,
        mode: str,
        col_rename_func: Optional[Callable[[Any], Any]] = None,
        col_mode: Optional[str] = None,
        nan_mode: Optional[str] = None,
    ) -> None:
        """
        :param nid: node identifier
        :param signal_cols: names of columns to (de)modulate
        :param volatility_col: name of volatility column
        :param signal_steps_ahead: steps ahead of the signal columns. If signal
            is at `t_0`, this value should be `0`. If signal is a forward
            prediction of z-scored returns indexed by knowledge time, this
            value should be equal to the number of steps of the prediction
        :param volatility_steps_ahead: steps ahead of the volatility column. If
            volatility column is an output of `SmaModel`, this corresponds to
            the `steps_ahead` parameter
        :param mode: "modulate" or "demodulate"
        :param col_rename_func: as in `ColumnTransformer`
        :param col_mode: as in `ColumnTransformer`
        """
        super().__init__(nid)
        self._signal_cols = self._to_list(signal_cols)
        self._volatility_col = volatility_col
        dbg.dassert_lte(0, signal_steps_ahead)
        self._signal_steps_ahead = signal_steps_ahead
        dbg.dassert_lte(0, volatility_steps_ahead)
        self._volatility_steps_ahead = volatility_steps_ahead
        dbg.dassert_in(mode, ["modulate", "demodulate"])
        self._mode = mode
        self._col_rename_func = col_rename_func or (lambda x: x)
        self._col_mode = col_mode or "replace_all"
        self._nan_mode = nan_mode or "leave_unchanged"

    def fit(self, df_in: pd.DataFrame) -> Dict[str, pd.DataFrame]:
        df_out = self._process_signal(df_in)
        info = collections.OrderedDict()
        info["df_out_info"] = get_df_info_as_string(df_out)
        self._set_info("fit", info)
        return {"df_out": df_out}

    def predict(self, df_in: pd.DataFrame) -> Dict[str, pd.DataFrame]:
        df_out = self._process_signal(df_in)
        info = collections.OrderedDict()
        info["df_out_info"] = get_df_info_as_string(df_out)
        self._set_info("predict", info)
        return {"df_out": df_out}

    def _process_signal(self, df_in: pd.DataFrame) -> pd.DataFrame:
        """
        Modulate or demodulate signal by volatility prediction.

        :param df_in: dataframe with `self._signal_cols` and
            `self._volatility_col` columns
        :return: adjusted signal indexed in the same way as the input signal
        """
        dbg.dassert_is_subset(self._signal_cols, df_in.columns.tolist())
        dbg.dassert_in(self._volatility_col, df_in.columns)
        fwd_signal = df_in[self._signal_cols]
        fwd_volatility = df_in[self._volatility_col]
        # Shift volatility to align it with signal.
        volatility_shift = self._volatility_steps_ahead - self._signal_steps_ahead
        if self._nan_mode == "drop":
            fwd_volatility = fwd_volatility.dropna()
        elif self._nan_mode == "leave_unchanged":
            pass
        else:
            raise ValueError(f"Unrecognized `nan_mode` {self._nan_mode}")
        volatility_aligned = fwd_volatility.shift(volatility_shift)
        # Adjust signal by volatility.
        if self._mode == "demodulate":
            adjusted_signal = fwd_signal.divide(volatility_aligned, axis=0)
        elif self._mode == "modulate":
            adjusted_signal = fwd_signal.multiply(volatility_aligned, axis=0)
        else:
            raise ValueError(f"Invalid mode=`{self._mode}`")
        df_out = self._apply_col_mode(
            df_in,
            adjusted_signal,
            cols=self._signal_cols,
            col_rename_func=self._col_rename_func,
            col_mode=self._col_mode,
        )
        return df_out


# #############################################################################
# gluon-ts - DeepAR
# #############################################################################


class ContinuousDeepArModel(FitPredictNode, RegFreqMixin, ToListMixin):
    """
    A dataflow node for a DeepAR model.

    This node trains a DeepAR model using only one time series
    - By using only one time series, we are not taking advantage of the
      "global" modeling capabilities of gluonts or DeepAR
    - This may be somewhat mitigated by the fact that the single time series
      that we provide will typically contain on the order of 10E5 or more time
      points
    - In training, DeepAR randomly cuts the time series provided, and so
      unless there are obvious cut-points we want to take advantage of, it may
      be best to let DeepAR cut
    - If certain cut-points are naturally more appropriate in our problem
      domain, an event study modeling approach may be more suitable

    See https://arxiv.org/abs/1704.04110 for a description of the DeepAR model.

    For additional context and best-practices, see
    https://github.com/.../.../issues/966
    """

    def __init__(
        self,
        nid: str,
        y_vars: _TO_LIST_MIXIN_TYPE,
        trainer_kwargs: Optional[Any] = None,
        estimator_kwargs: Optional[Any] = None,
        x_vars: Optional[_TO_LIST_MIXIN_TYPE] = None,
        num_traces: int = 100,
    ) -> None:
        """
        Initialize dataflow node for gluon-ts DeepAR model.

        :param nid: unique node id
        :param y_vars: Used in autoregression
        :param trainer_kwargs: See
          - https://gluon-ts.mxnet.io/api/gluonts/gluonts.trainer.html#gluonts.trainer.Trainer
          - https://github.com/awslabs/gluon-ts/blob/master/src/gluonts/trainer/_base.py
        :param estimator_kwargs: See
          - https://gluon-ts.mxnet.io/api/gluonts/gluonts.model.deepar.html
          - https://github.com/awslabs/gluon-ts/blob/master/src/gluonts/model/deepar/_estimator.py
        :param x_vars: Covariates. Could be, e.g., features associated with a
            point-in-time event. Must be known throughout the prediction
            window at the time the prediction is made. May be omitted.
        :num_traces: Number of sample paths / traces to generate per
            prediction. The mean of the traces is used as the prediction.
        """
        super().__init__(nid)
        self._estimator_kwargs = estimator_kwargs
        # To avoid passing a class through config, handle `Trainer()`
        # parameters separately from `estimator_kwargs`.
        self._trainer_kwargs = trainer_kwargs
        self._trainer = gtrain.Trainer(**self._trainer_kwargs)
        dbg.dassert_not_in("trainer", self._estimator_kwargs)
        #
        self._estimator_func = gmdeep.DeepAREstimator
        # NOTE: Covariates (x_vars) are not required by DeepAR.
        #   - This could be useful for, e.g., predicting future values of
        #     what would normally be predictors
        self._x_vars = x_vars
        self._y_vars = y_vars
        self._num_traces = num_traces
        self._estimator = None
        self._predictor = None
        #
        dbg.dassert_in("prediction_length", self._estimator_kwargs)
        self._prediction_length = self._estimator_kwargs["prediction_length"]
        dbg.dassert_lt(0, self._prediction_length)
        dbg.dassert_not_in(
            "freq",
            self._estimator_kwargs,
            "`freq` to be autoinferred from `df_in`; do not specify",
        )

    def fit(self, df_in: pd.DataFrame) -> Dict[str, pd.DataFrame]:
        self._validate_input_df(df_in)
        df = df_in.copy()
        # Obtain index slice for which forward targets exist.
        dbg.dassert_lt(self._prediction_length, df.index.size)
        df_fit = df.iloc[: -self._prediction_length]
        #
        if self._x_vars is not None:
            x_vars = self._to_list(self._x_vars)
        else:
            x_vars = None
        y_vars = self._to_list(self._y_vars)
        # Transform dataflow local timeseries dataframe into gluon-ts format.
        gluon_train = cdataa.transform_to_gluon(
            df_fit, x_vars, y_vars, df_fit.index.freq.freqstr
        )
        # Instantiate the (DeepAR) estimator and train the model.
        self._estimator = self._estimator_func(
            trainer=self._trainer,
            freq=df_fit.index.freq.freqstr,
            **self._estimator_kwargs,
        )
        self._predictor = self._estimator.train(gluon_train)
        # Predict. Generate predictions over all of `df_in` (not just on the
        #     restricted slice `df_fit`).
        fwd_y_hat, fwd_y = cbackt.generate_predictions(
            predictor=self._predictor,
            df=df,
            y_vars=y_vars,
            prediction_length=self._prediction_length,
            num_samples=self._num_traces,
            x_vars=x_vars,
        )
        # Store info.
        info = collections.OrderedDict()
        info["model_x_vars"] = x_vars
        #
        df_out = fwd_y.merge(fwd_y_hat, left_index=True, right_index=True)
        info["df_out_info"] = get_df_info_as_string(df_out)
        self._set_info("fit", info)
        dbg.dassert_no_duplicates(df_out.columns)
        return {"df_out": df_out}

    def predict(self, df_in: pd.DataFrame) -> Dict[str, pd.DataFrame]:
        self._validate_input_df(df_in)
        df = df_in.copy()
        if self._x_vars is not None:
            x_vars = self._to_list(self._x_vars)
        else:
            x_vars = None
        y_vars = self._to_list(self._y_vars)
        gluon_train = cdataa.transform_to_gluon(
            df, x_vars, y_vars, df.index.freq.freqstr
        )
        # Instantiate the (DeepAR) estimator and train the model.
        self._estimator = self._estimator_func(
            trainer=self._trainer,
            freq=df.index.freq.freqstr,
            **self._estimator_kwargs,
        )
        self._predictor = self._estimator.train(gluon_train)
        #
        fwd_y_hat, fwd_y = cbackt.generate_predictions(
            predictor=self._predictor,
            df=df,
            y_vars=y_vars,
            prediction_length=self._prediction_length,
            num_samples=self._num_traces,
            x_vars=x_vars,
        )
        # Store info.
        info = collections.OrderedDict()
        info["model_x_vars"] = x_vars
        #
        df_out = fwd_y.merge(fwd_y_hat, left_index=True, right_index=True)
        info["df_out_info"] = get_df_info_as_string(df_out)
        self._set_info("predict", info)
        dbg.dassert_no_duplicates(df_out.columns)
        return {"df_out": df_out}

    def _get_fwd_y_df(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Return dataframe of `steps_ahead` forward y values.
        """
        y_vars = self._to_list(self._y_vars)
        mapper = lambda y: str(y) + "_%i" % self._prediction_length
        # TODO(gp): Not sure if the following is needed.
        # [mapper(y) for y in y_vars]
        # TODO(Paul): Ensure that `fwd_y_vars` and `y_vars` do not overlap.
        fwd_y_df = (
            df[y_vars].shift(-self._prediction_length).rename(columns=mapper)
        )
        return fwd_y_df


class DeepARGlobalModel(FitPredictNode, ToListMixin):
    """
    A dataflow node for a DeepAR model.

    See https://arxiv.org/abs/1704.04110 for a description of the DeepAR model.

    For additional context and best-practices, see
    https://github.com/.../.../issues/966
    """

    def __init__(
        self,
        nid: str,
        x_vars: _TO_LIST_MIXIN_TYPE,
        y_vars: _TO_LIST_MIXIN_TYPE,
        trainer_kwargs: Optional[Any] = None,
        estimator_kwargs: Optional[Any] = None,
    ) -> None:
        """
        Initialize dataflow node for gluon-ts DeepAR model.

        :param nid: unique node id
        :param trainer_kwargs: See
          - https://gluon-ts.mxnet.io/api/gluonts/gluonts.trainer.html#gluonts.trainer.Trainer
          - https://github.com/awslabs/gluon-ts/blob/master/src/gluonts/trainer/_base.py
        :param estimator_kwargs: See
          - https://gluon-ts.mxnet.io/api/gluonts/gluonts.model.deepar.html
          - https://github.com/awslabs/gluon-ts/blob/master/src/gluonts/model/deepar/_estimator.py
        :param x_vars: Covariates. Could be, e.g., features associated with a
            point-in-time event. Must be known throughout the prediction
            window at the time the prediction is made.
        :param y_vars: Used in autoregression
        """
        super().__init__(nid)
        self._estimator_kwargs = estimator_kwargs
        # To avoid passing a class through config, handle `Trainer()`
        # parameters separately from `estimator_kwargs`.
        self._trainer_kwargs = trainer_kwargs
        self._trainer = gtrain.Trainer(**self._trainer_kwargs)
        dbg.dassert_not_in("trainer", self._estimator_kwargs)
        #
        self._estimator_func = gmdeep.DeepAREstimator
        # NOTE: Covariates (x_vars) are not required by DeepAR.
        # TODO(Paul): Allow this model to accept y_vars only.
        #   - This could be useful for, e.g., predicting future values of
        #     what would normally be predictors
        self._x_vars = x_vars
        self._y_vars = y_vars
        self._estimator = None
        self._predictor = None
        # We determine `prediction_length` automatically and therefore do not
        # allow it to be set by the user.
        dbg.dassert_not_in("prediction_length", self._estimator_kwargs)
        #
        dbg.dassert_in("freq", self._estimator_kwargs)
        self._freq = self._estimator_kwargs["freq"]

    def fit(self, df_in: pd.DataFrame) -> Dict[str, pd.DataFrame]:
        """
        Fit model to multiple series reflected in multiindexed `df_in`.

        `prediction_length` is autoinferred from the max index of `t_j`, e.g.,
        each `df_in` is assumed to include the index `0` for, e.g.,
        "event time", and indices are assumed to be consecutive integers. So
        if there are time points

            t_{-2} < t_{-1} < t_0 < t_1 < t_2

        then `prediction_length = 2`.
        """
        dbg.dassert_isinstance(df_in, pd.DataFrame)
        dbg.dassert_no_duplicates(df_in.columns)
        x_vars = self._to_list(self._x_vars)
        y_vars = self._to_list(self._y_vars)
        df = df_in.copy()
        # Transform dataflow local timeseries dataframe into gluon-ts format.
        gluon_train = cdataa.transform_to_gluon(df, x_vars, y_vars, self._freq)
        # Set the prediction length to the length of the local timeseries - 1.
        #   - To predict for time t_j at time t_i, t_j > t_i, we need to know
        #     x_vars up to and including time t_j
        #   - For this model, multi-step predictions are equivalent to
        #     iterated single-step predictions
        self._prediction_length = df.index.get_level_values(0).max()
        # Instantiate the (DeepAR) estimator and train the model.
        self._estimator = self._estimator_func(
            prediction_length=self._prediction_length,
            trainer=self._trainer,
            **self._estimator_kwargs,
        )
        self._predictor = self._estimator.train(gluon_train)
        # Apply model predictions to the training set (so that we can evaluate
        # in-sample performance).
        #   - Include all data points up to and including zero (the event time)
        gluon_test = cdataa.transform_to_gluon(
            df, x_vars, y_vars, self._freq, self._prediction_length
        )
        fit_predictions = list(self._predictor.predict(gluon_test))
        # Transform gluon-ts predictions into a dataflow local timeseries
        # dataframe.
        # TODO(Paul): Gluon has built-in functionality to take the mean of
        #     traces, and we might consider using it instead.
        y_hat_traces = cdataa.transform_from_gluon_forecasts(fit_predictions)
        # TODO(Paul): Store the traces / dispersion estimates.
        # Average over all available samples.
        y_hat = y_hat_traces.mean(level=[0, 1])
        # Map multiindices to align our prediction indices with those used
        # by the passed-in local timeseries dataframe.
        # TODO(Paul): Do this mapping earlier before removing the traces.
        aligned_idx = y_hat.index.map(
            lambda x: (
                x[0] + 1,
                x[1] - pd.Timedelta(f"1{self._freq}"),
            )
        )
        y_hat.index = aligned_idx
        y_hat.name = str(y_vars[0]) + "_hat"
        y_hat.index.rename(df.index.names, inplace=True)
        # Store info.
        info = collections.OrderedDict()
        info["model_x_vars"] = x_vars
        # TODO(Paul): Consider storing only the head of each list in `info`
        #     for debugging purposes.
        # info["gluon_train"] = list(gluon_train)
        # info["gluon_test"] = list(gluon_test)
        # info["fit_predictions"] = fit_predictions
        df_out = y_hat.to_frame()
        info["df_out_info"] = get_df_info_as_string(df_out)
        self._set_info("fit", info)
        return {"df_out": df_out}

    def predict(self, df_in: pd.DataFrame) -> Dict[str, pd.DataFrame]:
        dbg.dassert_isinstance(df_in, pd.DataFrame)
        dbg.dassert_no_duplicates(df_in.columns)
        x_vars = self._to_list(self._x_vars)
        y_vars = self._to_list(self._y_vars)
        df = df_in.copy()
        # Transform dataflow local timeseries dataframe into gluon-ts format.
        gluon_test = cdataa.transform_to_gluon(
            df,
            x_vars,
            y_vars,
            self._freq,
            self._prediction_length,
        )
        predictions = list(self._predictor.predict(gluon_test))
        # Transform gluon-ts predictions into a dataflow local timeseries
        # dataframe.
        # TODO(Paul): Gluon has built-in functionality to take the mean of
        #     traces, and we might consider using it instead.
        y_hat_traces = cdataa.transform_from_gluon_forecasts(predictions)
        # TODO(Paul): Store the traces / dispersion estimates.
        # Average over all available samples.
        y_hat = y_hat_traces.mean(level=[0, 1])
        # Map multiindices to align our prediction indices with those used
        # by the passed-in local timeseries dataframe.
        # TODO(Paul): Do this mapping earlier before removing the traces.
        aligned_idx = y_hat.index.map(
            lambda x: (
                x[0] + 1,
                x[1] - pd.Timedelta(f"1{self._freq}"),
            )
        )
        y_hat.index = aligned_idx
        y_hat.name = str(y_vars[0]) + "_hat"
        y_hat.index.rename(df.index.names, inplace=True)
        # Store info.
        info = collections.OrderedDict()
        info["model_x_vars"] = x_vars
        # TODO(Paul): Consider storing only the head of each list in `info`
        #     for debugging purposes.
        # info["gluon_train"] = list(gluon_train)
        # info["gluon_test"] = list(gluon_test)
        # info["fit_predictions"] = fit_predictions
        df_out = y_hat.to_frame()
        info["df_out_info"] = get_df_info_as_string(df_out)
        self._set_info("predict", info)
        return {"df_out": df_out}


# #############################################################################
# statsmodels - SARIMAX
# #############################################################################


class ContinuousSarimaxModel(
    FitPredictNode, RegFreqMixin, ToListMixin, ColModeMixin
):
    """
    A dataflow node for continuous SARIMAX model.

    This is a wrapper around statsmodels SARIMAX with the following
    modifications:
      - We predict `y_t`, ... , `y_{t+n}` using `x_{t-n}`, ..., `x_{t-1}`,
        where `n` is `steps_ahead`, whereas in the classic implementation it is
        predicted using`x_t`, ... , `x_{t+n}`
      - When making predictions in the `fit` method, we treat the input data as
        out-of-sample. This allows making n-step-ahead predictions on a subset
        of exogenous data.

    See
    https://www.statsmodels.org/stable/examples/notebooks/generated/statespace_sarimax_stata.html
    for SARIMAX model examples.
    """

    # pylint: disable=too-many-ancestors

    def __init__(
        self,
        nid: str,
        y_vars: _TO_LIST_MIXIN_TYPE,
        steps_ahead: int,
        init_kwargs: Optional[Dict[str, Any]] = None,
        fit_kwargs: Optional[Dict[str, Any]] = None,
        x_vars: Optional[_TO_LIST_MIXIN_TYPE] = None,
        add_constant: bool = False,
        col_mode: Optional[str] = None,
        nan_mode: Optional[str] = None,
        disable_tqdm: bool = False,
    ) -> None:
        """
        Initialize node for SARIMAX model.

        :param nid: node identifier
        :param y_vars: names of y variables. Only univariate predictions are
            supported
        :param steps_ahead: number of prediction steps
        :param init_kwargs: kwargs for initializing `sm.tsa.statespace.SARIMAX`
        :param fit_kwargs: kwargs for `fit` method of
            `sm.tsa.statespace.SARIMAX`
        :param x_vars: names of x variables
        :param add_constant: whether to add constant to exogenous dataset,
            default `False`. Note that adding a constant and specifying
            `trend="c"` is not the same
        :param col_mode: "replace_all" or "merge_all"
        :param nan_mode: "raise", "drop" or "leave_unchanged"
        :param disable_tqdm: whether to disable tqdm progress bar
        """
        super().__init__(nid)
        self._y_vars = y_vars
        # Zero-step prediction is not supported for autoregressive models.
        dbg.dassert_lte(
            1, steps_ahead, "Non-causal prediction attempted! Aborting..."
        )
        self._steps_ahead = steps_ahead
        self._init_kwargs = init_kwargs
        self._fit_kwargs = fit_kwargs or {"disp": False}
        self._x_vars = x_vars
        self._add_constant = add_constant
        self._model = None
        self._model_results = None
        self._col_mode = col_mode or "merge_all"
        dbg.dassert_in(self._col_mode, ["replace_all", "merge_all"])
        self._nan_mode = nan_mode or "raise"
        self._disable_tqdm = disable_tqdm

    def fit(self, df_in: pd.DataFrame) -> Dict[str, pd.DataFrame]:
        self._validate_input_df(df_in)
        df = df_in.copy()
        idx = df.index
        # Get intersection of non-NaN `y` and `x`.
        y_vars = self._to_list(self._y_vars)
        y_fit = df[y_vars]
        if self._nan_mode == "leave_unchanged":
            non_nan_idx = idx
        else:
            non_nan_idx = df[y_vars].dropna().index
        if self._x_vars is not None:
            x_fit = self._get_bkwd_x_df(df).dropna()
            x_fit_non_nan_idx = x_fit.index
            x_idx_extension = x_fit_non_nan_idx[-self._steps_ahead :]
            non_nan_idx = non_nan_idx.append(x_idx_extension)
            non_nan_idx = non_nan_idx.intersection(x_fit_non_nan_idx)
            idx = idx[self._steps_ahead :].append(x_idx_extension)
        else:
            x_fit = None
        x_fit = self._add_constant_to_x(x_fit)
        dbg.dassert(not non_nan_idx.empty)
        # Handle presence of NaNs according to `nan_mode`.
        self._handle_nans(idx, non_nan_idx)
        y_fit = y_fit.reindex(non_nan_idx)
        # Fit the model.
        self._model = sm.tsa.statespace.SARIMAX(
            y_fit, exog=x_fit, **self._init_kwargs
        )
        self._model_results = self._model.fit(**self._fit_kwargs)
        # Get predictions.
        # Treat the fit data as out-of-sample to mimic the behavior in
        # `predict()`.
        fwd_y_hat = self._predict(y_fit, x_fit)
        # Package results.
        fwd_y_df = self._get_fwd_y_df(df)
        df_out = fwd_y_df.merge(
            fwd_y_hat, how="outer", left_index=True, right_index=True
        )
        df_out = self._apply_col_mode(
            df, df_out, cols=self._to_list(self._y_vars), col_mode=self._col_mode
        )
        # Add info.
        # TODO(Julia): Maybe add model performance to info.
        info = collections.OrderedDict()
        info["model_summary"] = _remove_datetime_info_from_sarimax(
            _convert_sarimax_summary_to_dataframe(self._model_results.summary())
        )
        info["df_out_info"] = get_df_info_as_string(df_out)
        self._set_info("fit", info)
        return {"df_out": df_out}

    def predict(self, df_in: pd.DataFrame) -> Dict[str, pd.DataFrame]:
        self._validate_input_df(df_in)
        df = df_in.copy()
        idx = df.index
        # Get intersection of non-NaN `y` and `x`.
        y_vars = self._to_list(self._y_vars)
        dbg.dassert_eq(len(y_vars), 1, "Only univariate `y` is supported")
        y_predict = df[y_vars]
        if self._x_vars is not None:
            x_predict = self._get_bkwd_x_df(df)
            x_predict = x_predict.dropna()
            y_predict = y_predict.reindex(x_predict.index)
            x_idx_extension = x_predict.index[-self._steps_ahead :]
            idx = idx[self._steps_ahead :].append(x_idx_extension)
        else:
            x_predict = None
        x_predict = self._add_constant_to_x(x_predict)
        # Handle presence of NaNs according to `nan_mode`.
        self._handle_nans(idx, y_predict.index)
        fwd_y_hat = self._predict(y_predict, x_predict)
        # Package results.
        fwd_y_df = self._get_fwd_y_df(df)
        df_out = fwd_y_df.merge(
            fwd_y_hat, how="outer", left_index=True, right_index=True
        )
        df_out = self._apply_col_mode(
            df, df_out, cols=self._to_list(self._y_vars), col_mode=self._col_mode
        )
        # Add info.
        info = collections.OrderedDict()
        info["model_summary"] = _remove_datetime_info_from_sarimax(
            _convert_sarimax_summary_to_dataframe(self._model_results.summary())
        )
        info["df_out_info"] = get_df_info_as_string(df_out)
        self._set_info("predict", info)
        return {"df_out": df_out}

    def _predict(
        self, y: pd.DataFrame, x: Optional[pd.DataFrame]
    ) -> pd.DataFrame:
        """
        Make n-step-ahead predictions.
        """
        preds = []
        if self._x_vars is not None:
            pred_range = len(y) - self._steps_ahead
            # TODO(Julia): Check this.
            pred_start = self._steps_ahead
        else:
            pred_range = len(y)
            pred_start = 1
        y_var = y.columns[0]
        for t in tqdm(
            range(pred_start, pred_range + 1), disable=self._disable_tqdm
        ):
            # If `t` is larger than `y`, this selects the whole `y`.
            y_past = y.iloc[:t]
            if x is not None:
                x_past = x.iloc[:t]
                x_step = x.iloc[t : t + self._steps_ahead]
            else:
                x_past = None
                x_step = None
            # Create a model with the same params as `self._model`, but make it
            # aware of the past values.
            model_predict = self._model.clone(y_past, exog=x_past)
            result_predict = model_predict.filter(self._model_results.params)
            # Make forecast.
            forecast = result_predict.forecast(
                steps=self._steps_ahead, exog=x_step
            )
            # Transform forecast into a row indexed by prediction time.
            forecast = forecast.to_frame(name=y_past.index[-1]).T
            forecast.columns = [
                f"{y_var}_{i+1}_hat" for i in range(self._steps_ahead)
            ]
            preds.append(forecast)
        preds = pd.concat(preds)
        return preds

    def _get_bkwd_x_df(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Return dataframe of `steps_ahead - 1` backward x values.

        This way we predict `y_t` using `x_{t-n}`, ..., `x_{t-1}`, where `n` is
        `self._steps_ahead`.
        """
        x_vars = self._to_list(self._x_vars)
        shift = self._steps_ahead
        # Shift index instead of series to extend the index.
        bkwd_x_df = df[x_vars].copy()
        bkwd_x_df.index = bkwd_x_df.index.shift(shift)
        mapper = lambda y: str(y) + "_bkwd_%i" % shift
        return bkwd_x_df.rename(columns=mapper)

    def _get_fwd_y_df(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Return dataframe of `steps_ahead` forward y values.
        """
        y_vars = self._to_list(self._y_vars)
        mapper = lambda y: str(y) + "_%i" % self._steps_ahead
        fwd_y_df = df[y_vars].shift(-self._steps_ahead).rename(columns=mapper)
        return fwd_y_df

    def _add_constant_to_x(self, x: pd.DataFrame) -> Optional[pd.DataFrame]:
        if not self._add_constant:
            return x
        if self._x_vars is not None:
            self._x_vars = self._to_list(self._x_vars)
            dbg.dassert_not_in(
                "const",
                self._x_vars,
                "A column name 'const' is already present, please rename column.",
            )
            self._x_vars.append("const")
            x = sm.add_constant(x)
            return x
        _LOG.warning("`add_constant=True` but no exog is provided.")
        return x

    def _handle_nans(
        self, idx: pd.DataFrame.index, non_nan_idx: pd.DataFrame.index
    ) -> None:
        if self._nan_mode == "raise":
            if idx.shape[0] != non_nan_idx.shape[0]:
                nan_idx = idx.difference(non_nan_idx)
                raise ValueError(f"NaNs detected at {nan_idx}")
        elif self._nan_mode == "drop" or self._nan_mode == "leave_unchanged":
            pass
        else:
            raise ValueError(f"Unrecognized nan_mode `{self._nan_mode}`")


class MultihorizonReturnsPredictionProcessor(FitPredictNode, ToListMixin):
    """
    Process multi-horizon returns prediction.

    In multi-horizon returns prediction problem, the output can take a form of
    single-step returns for each forecast step. To get returns from `t_0` to
    `t_n`, we need to:
      - (optional): undo z-scoring
      - accumulate returns for each step. The output is `cumret_t_1, ...,
        cumret_t_n` and `cumret_t_1_hat, ..., cumret_t_n_hat`
    """

    def __init__(
        self,
        nid: str,
        target_col: Any,
        prediction_cols: _TO_LIST_MIXIN_TYPE,
        volatility_col: Any,
    ):
        """
        :param nid: node identifier
        :param target_col: name of the prediction target column which contains
            single-step returns, e.g. "ret_0_zscored"
        :param prediction_cols: name of columns with single-step returns
            predictions for each forecast step. The columns should be indexed
            by knowledge time and ordered by forecast step, e.g.
            `["ret_0_zscored_1_hat", "ret_0_zscored_2_hat",
            "ret_0_zscored_3_hat"]`
        :param volatility_col: name of a column containing one step ahead
            volatility forecast. If `None`, z-scoring is not inverted
        """
        super().__init__(nid)
        self._target_col = target_col
        self._prediction_cols = self._to_list(prediction_cols)
        self._volatility_col = volatility_col
        self._max_steps_ahead = len(self._prediction_cols)

    def fit(self, df_in: pd.DataFrame) -> Dict[str, pd.DataFrame]:
        df_out = self._process(df_in)
        info = collections.OrderedDict()
        info["df_out_info"] = get_df_info_as_string(df_out)
        self._set_info("fit", info)
        return {"df_out": df_out}

    def predict(self, df_in: pd.DataFrame) -> Dict[str, pd.DataFrame]:
        df_out = self._process(df_in)
        info = collections.OrderedDict()
        info["df_out_info"] = get_df_info_as_string(df_out)
        self._set_info("predict", info)
        return {"df_out": df_out}

    def _process(self, df_in: pd.DataFrame) -> pd.DataFrame:
        cum_ret_hat = self._process_predictions(df_in)
        fwd_cum_ret = self._process_target(df_in)
        # TODO(Julia): Add `col_mode`.
        cum_y_yhat = fwd_cum_ret.join(cum_ret_hat, how="right")
        return cum_y_yhat

    def _process_predictions(self, df_in: pd.DataFrame) -> pd.DataFrame:
        """
        Invert z-scoring and accumulate predicted returns for each step.
        """
        predictions = df_in[self._prediction_cols]
        # Invert z-scoring.
        if self._volatility_col is not None:
            vol_1_hat = df_in[self._volatility_col]
            predictions = predictions.multiply(vol_1_hat, axis=0)
        # Accumulate predicted returns for each step.
        cum_ret_hats = []
        for i in range(1, self._max_steps_ahead + 1):
            cum_ret_hat_curr = predictions.iloc[:, :i].sum(axis=1, skipna=False)
            cum_ret_hats.append(cum_ret_hat_curr.to_frame(name=f"cumret_{i}_hat"))
        return pd.concat(cum_ret_hats, axis=1)

    def _process_target(self, df_in: pd.DataFrame) -> pd.Series:
        """
        Invert z-scoring and accumulate returns for each step.
        """
        target = df_in[self._target_col]
        # Invert z-scoring.
        if self._volatility_col is not None:
            vol_1_hat = df_in[self._volatility_col]
            vol_0_hat = vol_1_hat.shift(1)
            target = target * vol_0_hat
        # Accumulate target for each step.
        cum_rets = []
        for i in range(1, self._max_steps_ahead + 1):
            cum_ret_curr = csigna.accumulate(target, i).rename(f"cumret_{i}")
            cum_rets.append(cum_ret_curr)
        cum_rets = pd.concat(cum_rets, axis=1)
        fwd_cum_ret = cum_rets.shift(-self._max_steps_ahead)
        return fwd_cum_ret


def _convert_sarimax_summary_to_dataframe(
    summary: siolib.summary.Summary,
) -> Dict[str, pd.DataFrame]:
    """
    Convert SARIMAX model summary to dataframes.

    SARIMAX model summary consists of 3 tables:
        1) general info - 2 columns of pairs key-value
        2) coefs - a table with rows for every feature
        3) tests results - 2 columns of pairs key-value
    Function converts 1 and 3 tables to a dataframe with index-key, column-value,
    2 table - a dataframe with index-feature.
    """
    tables_dict = collections.OrderedDict()
    keys = ["info", "coefs", "tests"]
    for i, table in enumerate(summary.tables):
        table = np.array(table)
        if table.shape[1] == 4:
            # Process paired tables.
            table = np.vstack((table[:, :2], table[:, 2:]))
            df = pd.DataFrame(table)
            df = df.applymap(lambda x: str(x).strip(": ").lstrip(" "))
            if "Sample" in df[0].values:
                sample_index = df[0].tolist().index("Sample")
                df.iloc[sample_index, 0] = "Start Date"
                df.iloc[sample_index + 1, 0] = "End Date"
                df.iloc[sample_index + 1, 1] = df.iloc[
                    sample_index + 1, 1
                ].lstrip("- ")
            df = df[df[0] != ""]
            df = df.set_index(0)
            df.index.name = None
            df = df.iloc[:, 0]
        else:
            # Process coefs table.
            df = pd.DataFrame(table[1:, :])
            df = df.set_index(0)
            df.index.name = None
            df.columns = table[0, 1:]
        tables_dict[keys[i]] = df
    return tables_dict


def _remove_datetime_info_from_sarimax(
    summary: Dict[str, pd.DataFrame],
) -> Dict[str, pd.DataFrame]:
    """
    Remove date and time from model summary.
    """
    summary["info"] = summary["info"].drop(["Date", "Time"])
    return summary
