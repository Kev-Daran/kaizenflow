#!/usr/bin/env python
"""
Generate daily PQ files.

# Example:
> im_v2/common/data/transform/test/generate_pq_example_data.py \
    --start_date 2021-11-23 \
    --end_date 2021-11-25 \
    --assets A,B,C \
    --dst_dir im_v2/common/data/transform/test_data_by_date

Import as:

import im_v2.common.data.transform.generate_pq_example_data as imvcdtgped
"""

import argparse
import logging
from typing import List

import pandas as pd

import helpers.dbg as hdbg
import helpers.hparquet as hparque
import helpers.parser as hparser
import helpers.printing as hprint

_LOG = logging.getLogger(__name__)


def _get_generic_daily_df(
    start_date: str, end_date: str, assets: List[str], freq: str
) -> pd.DataFrame:
    """
    Create data for the interval [start_date, end_date].

    :param start_date: start of date range including start_date
    :param end_date: end of date range excluding end_date
    :param assets: list of desired assets
    :param freq: frequency of steps between start and end date
    :return: daily dataframe as presented below
    ```
                idx asset  val1  val2
    2000-01-01    0     A    00    00
    2000-01-02    0     A    01    01
    2000-01-03    0     A    02    02
    ```
    """
    df_idx = pd.date_range(start_date, end_date, freq=freq)
    _LOG.debug("df_idx=[%s, %s]", min(df_idx), max(df_idx))
    _LOG.debug("len(df_idx)=%s", len(df_idx))
    # For each asset generate random data.
    df = []
    for idx, asset in enumerate(assets):
        df_tmp = pd.DataFrame(
            {
                "idx": idx,
                "asset": asset,
                "val1": list(range(len(df_idx))),
                "val2": list(range(len(df_idx))),
            },
            index=df_idx,
        )
        # Drop last midnight.
        # TODO(Nikola): end_date - pd.DateOffset(days=1)
        df_tmp.drop(df_tmp.tail(1).index, inplace=True)
        _LOG.debug(hprint.df_to_short_str("df_tmp", df_tmp))
        df.append(df_tmp)
    # Create a single df for all the assets.
    df = pd.concat(df)
    _LOG.debug(hprint.df_to_short_str("df", df))
    return df


# TODO(Nikola): Unify with func above or randomize data further.
def _get_verbose_daily_df(
    start_date: str, end_date: str, assets: List[str], freq: str
) -> pd.DataFrame:
    """
    Create data for the interval [start_date, end_date].

    :param start_date: start of date range including start_date
    :param end_date: end of date range excluding end_date
    :param assets: list of desired assets
    :param freq: frequency of steps between start and end date
    :return: daily dataframe as presented below
    ```
    vendor_date  interval  start_time    end_time ticker currency  open    id
     2021-11-24        60  1637762400  1637762460      A      USD   100    1
     2021-11-24        60  1637762400  1637762460      A      USD   200    2
    ```
    """
    df_idx = pd.date_range(start_date, end_date, freq=freq)
    interval = df_idx[1] - df_idx[0]
    interval = interval.seconds
    _LOG.debug("df_idx=[%s, %s]", min(df_idx), max(df_idx))
    _LOG.debug("len(df_idx)=%s", len(df_idx))
    # For each asset generate random data.
    df = []
    for idx, asset in enumerate(assets):
        df_tmp = pd.DataFrame(
            {
                "interval": interval,
                "start_time": None,
                "end_time": None,
                "ticker": asset,
                "currency": "USD",
                "open": list(range(len(df_idx))),
                "id": idx,
            },
            index=df_idx,
        )
        # Drop last midnight.
        # TODO(Nikola): end_date - pd.DateOffset(days=1)
        df_tmp.drop(df_tmp.tail(1).index, inplace=True)
        _LOG.debug(hprint.df_to_short_str("df_tmp", df_tmp))
        df.append(df_tmp)
    # Create a single df for all the assets.
    df = pd.concat(df)
    start_time = (df.index - pd.Timestamp("1970-01-01")) // pd.Timedelta("1s")
    end_time = start_time + interval
    # TODO(Nikola): Handle various types of dates?
    # df.index = df.index.date
    df["start_time"] = start_time
    df["end_time"] = end_time
    df.index.name = "vendor_date"
    _LOG.debug(hprint.df_to_short_str("df", df))
    return df


def _parse() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--start_date",
        action="store",
        type=str,
        required=True,
        help="From when is data going to be created, including start date",
    )
    parser.add_argument(
        "--end_date",
        action="store",
        type=str,
        required=True,
        help="Until when is data going to be created, excluding end date",
    )
    parser.add_argument(
        "--assets",
        action="store",
        type=str,
        required=True,
        help="Comma separated string of assets",
    )
    parser.add_argument(
        "--dst_dir",
        action="store",
        type=str,
        required=True,
        help="Location that will be used to store generated data",
    )
    parser.add_argument(
        "--freq",
        action="store",
        type=str,
        help="Frequency of data generation. Defaults to one hour",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="More realistic and complete data is generated",
    )
    hparser.add_verbosity_arg(parser)
    return parser


def _main(parser: argparse.ArgumentParser) -> None:
    """
    Standard main part of the script that is parsing provided arguments.

    Timespan provided via start and end date, can not start and end on
    the same day. Start date is included in timespan, while end date is
    excluded.
    """
    args = parser.parse_args()
    hdbg.init_logger(verbosity=args.log_level, use_exec_path=True)
    # Generation timespan.
    start_date = args.start_date
    end_date = args.end_date
    hdbg.dassert_lt(start_date, end_date)
    timespan = pd.date_range(start_date, end_date)
    hdbg.dassert_lt(2, len(timespan))
    assets = args.assets
    assets = assets.split(",")
    dst_dir = args.dst_dir
    freq = args.freq if args.freq else "1H"
    get_daily_df = (
        _get_verbose_daily_df if args.verbose else _get_generic_daily_df
    )
    dummy_df = get_daily_df(start_date, end_date, assets, freq)
    hparque.save_daily_df_as_pq(dummy_df, dst_dir)


if __name__ == "__main__":
    _main(_parse())