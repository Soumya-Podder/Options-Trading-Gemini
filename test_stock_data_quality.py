import argparse
import json
import logging
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests
import dhan_dynamic_fno_stock_bot as bot
from dhan_dynamic_fno_stock_bot import IST, quote_received_age


def candles(stamp, strike=False):
    data={"timestamp":[int(stamp)],"open":[100],"high":[102],"low":[99],"close":[101]}
    data["strike" if strike else "volume"]=[1000]
    return data


def response(data,status=200):
    result=MagicMock(status_code=status)
    result.json.return_value=data
    return result


class HistoricalDataTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.client=bot.DhanStockOptionsClient("test","not-a-real-token",logging.getLogger("history-test"))
        self.client.logger=MagicMock()
        self.client.history_cache_dir=self.folder.name
        self.client.session=MagicMock()
        self.sleep=patch("dhan_dynamic_fno_stock_bot.time.sleep")
        self.sleep.start();self.addCleanup(self.sleep.stop)
        self.stamp=datetime(2026,9,18,14,40,tzinfo=IST).timestamp()

    def fetch(self):
        return self.client.fetch_intraday_candles("1",from_date="2026-09-18",to_date="2026-09-19",use_cache=True)

    def test_old_cache_ignored_and_completed_period_cache_reused(self):
        Path(self.folder.name,"legacy.json").write_text(json.dumps(candles(self.stamp-86400)))
        self.client.session.post.return_value=response(candles(self.stamp))
        first=self.fetch();second=self.fetch()
        self.assertEqual(first,second)
        self.client.session.post.assert_called_once()
        stored=json.loads(next(Path(self.folder.name).glob("history_v2_*.json")).read_text())
        self.assertEqual(stored["version"],2)
        self.assertIn("received_epoch",stored)

    def test_missing_after_1445_does_not_mark_active_session_tail_missing(self):
        self.client.session.post.return_value=response(candles(self.stamp))
        self.assertIsNotNone(self.fetch())
        self.assertEqual(self.client.history_events, [])

    def test_missing_before_1445_remains_a_coverage_warning(self):
        self.client.session.post.return_value=response(candles(self.stamp-300))
        self.assertIsNotNone(self.fetch())
        self.assertEqual(self.client.history_events[-1]['reason'], 'partial_session_tail')

    def test_partial_final_cache_is_refetched_instead_of_frozen(self):
        self.client.session.post.return_value=response(candles(self.stamp))
        self.fetch()
        path=next(Path(self.folder.name).glob("history_v2_*.json"))
        saved=json.loads(path.read_text());saved["data"]["timestamp"][0]-=3600;path.write_text(json.dumps(saved))
        self.assertEqual(self.fetch()["timestamp"],[int(self.stamp)])
        self.assertEqual(self.client.session.post.call_count,2)
        self.assertEqual(self.client.history_events,[])

    def test_mutable_period_expired_cache_is_refetched(self):
        now=datetime.now(IST).replace(microsecond=0)
        start=(now-timedelta(hours=1)).isoformat();end=(now+timedelta(seconds=1)).isoformat()
        self.client.session.post.return_value=response(candles(now.timestamp()-300))
        self.client.fetch_intraday_candles("1",from_date=start,to_date=end,use_cache=True)
        path=next(Path(self.folder.name).glob("history_v2_*.json"));d=json.loads(path.read_text());d["received_epoch"]-=60;path.write_text(json.dumps(d))
        self.client.fetch_intraday_candles("1",from_date=start,to_date=end,use_cache=True)
        self.assertEqual(self.client.session.post.call_count,2)

    def test_exclusive_end_filters_current_day(self):
        data=candles(self.stamp)
        later=candles(datetime(2026,9,19,9,15,tzinfo=IST).timestamp())
        for k in data:data[k]+=later[k]
        self.client.session.post.return_value=response(data)
        self.assertEqual(self.fetch()["timestamp"],[int(self.stamp)])
        sent=self.client.session.post.call_args.kwargs["json"]
        self.assertEqual(sent["toDate"],"2026-09-19 00:00:00")

    def test_rolling_chunks_merge_and_exclude_boundary_duplicates(self):
        def post(url,**kwargs):
            payload=kwargs["json"]
            start=datetime.fromisoformat(payload["fromDate"]).replace(tzinfo=IST)
            end=datetime.fromisoformat(payload["toDate"]).replace(tzinfo=IST)
            self.assertLessEqual((end-start).days,5)
            self.assertEqual(payload["requiredData"],["open","high","low","close","strike"])
            data=candles(start.timestamp()+9*3600+15*60,True)
            outside=candles(end.timestamp(),True)
            for k in data:data[k]+=outside[k]
            return response({"data":{"ce":data}})
        self.client.session.post.side_effect=post
        result=self.client.fetch_rolling_stock_option("1","CE","2026-09-01","2026-09-12")
        self.assertEqual(len(result["timestamp"]),3)
        self.assertEqual(len(set(result["timestamp"])),3)
        self.assertEqual(self.client.session.post.call_count,3)

    def test_timeout_retries_open_circuit_and_never_cache_empty(self):
        self.client.session.post.side_effect=requests.ReadTimeout("secret-content-must-not-be-logged")
        result=self.client.fetch_rolling_stock_option("1","CE","2026-09-01","2026-10-01")
        self.assertIsNone(result)
        self.assertEqual(self.client.session.post.call_count,6)
        self.assertEqual(len(list(Path(self.folder.name).glob("*.json"))),0)
        self.assertEqual(self.client.history_events[-1]["reason"],"circuit_open")
        self.assertNotIn("secret-content",json.dumps(self.client.history_events))

    def test_429_is_retried_and_success_does_not_mark_coverage_missing(self):
        self.client.session.post.side_effect=[response({"errorCode":"805"},429),response(candles(self.stamp))]
        self.assertIsNotNone(self.fetch())
        self.assertEqual(self.client.history_events,[])

    def test_live_history_recovers_after_bounded_circuit_cooldown(self):
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):return cls(2026,10,23,10,tzinfo=IST)
        self.client.history_failure_streak=3
        self.client.history_circuit_opened_at=100
        now=Clock.now(IST)
        stamp=now.replace(hour=9,minute=15,second=0,microsecond=0).timestamp()
        self.client.session.post.return_value=response(candles(stamp))
        with patch.object(bot,'datetime',Clock),patch.object(bot.time,'monotonic',return_value=150):
            self.assertIsNone(self.client.fetch_intraday_candles('1'))
        self.client.session.post.assert_not_called()
        with patch.object(bot,'datetime',Clock),patch.object(bot.time,'monotonic',return_value=161):
            self.assertIsNotNone(self.client.fetch_intraday_candles('1'))
        self.client.session.post.assert_called_once()
        self.assertEqual(self.client.history_failure_streak,0)

    def test_permanent_http_error_records_code_without_retry(self):
        self.client.session.post.return_value=response({"errorCode":"DH-902","errorType":"Invalid_Access"},401)
        self.assertIsNone(self.fetch())
        self.client.session.post.assert_called_once()
        self.assertEqual(self.client.history_events[0]["errorCode"],"DH-902")

    def test_known_partial_session_is_marked_incomplete(self):
        self.client.session.post.return_value=response(candles(self.stamp-3600))
        self.assertIsNotNone(self.fetch())
        self.assertEqual(self.client.history_events[0]["reason"],"partial_session_tail")
        self.assertEqual(self.client.history_coverage[0]["source"],"network")
        self.assertIn("last_bar",self.client.history_coverage[0])
        self.assertFalse(list(Path(self.folder.name).glob("*.json")))

    def test_empty_or_malformed_data_never_saved(self):
        for data in ({}, {**candles(self.stamp),"close":[]}, {**candles(self.stamp),"high":[float("nan")]}):
            self.client.session.post.return_value=response(data)
            self.assertIsNone(self.fetch())
        self.assertFalse(list(Path(self.folder.name).glob("*.json")))
        self.assertTrue(all(e["reason"]=="invalid_or_empty_data" for e in self.client.history_events))


class FreshnessTests(unittest.TestCase):
    def test_receipt_age_is_independent_of_last_trade_age(self):
        now=datetime(2026,10,22,11,0,tzinfo=IST).timestamp()
        quote={"received_epoch":now,"last_trade_time":"21/10/2026 10:00:00"}
        self.assertEqual(quote_received_age(quote,now),0)
        self.assertEqual(quote_received_age({**quote,"received_epoch":now-31},now),31)
        self.assertLess(quote_received_age({**quote,"received_epoch":now+1},now),0)

    def test_api_stamps_quote_and_chain_receipt(self):
        client=bot.DhanStockOptionsClient("test","test",MagicMock())
        client.session=MagicMock()
        client.session.post.return_value=response({"data":{"NSE_FNO":{"11":{"last_trade_time":"21/10/2026 10:00:00"}}}})
        with patch.object(bot.time,"sleep"),patch.object(bot.time,"time",return_value=123):
            self.assertEqual(client.fetch_option_quotes([11])["11"]["received_epoch"],123)
            client.session.post.return_value=response({"data":{"oc":{"100":{"ce":{"greeks":{}}}}}})
            self.assertEqual(client.fetch_option_chain(1,"2026-11-23")["100"]["ce"]["received_epoch"],123)


class BacktestCoverageTests(unittest.TestCase):
    def run_case(self, missing, partial=False, scored=False):
        class End(datetime):
            @classmethod
            def now(cls,tz=None):return cls(2026,9,19,tzinfo=tz)
        previous=candles(datetime(2026,9,17,15,25,tzinfo=IST).timestamp())
        latest=candles(datetime(2026,9,18,9,25,tzinfo=IST).timestamp())
        latest.update(open=[103],high=[105],low=[102],close=[104])
        for key in previous:previous[key]+=latest[key]
        client=MagicMock()
        client.fetch_intraday_candles.return_value=previous
        client.fetch_rolling_stock_option.return_value=None if missing else {**latest,"timestamp":[latest["timestamp"][0]+300],"open":[2],"strike":[1000]}
        if scored:
            client.fetch_rolling_stock_option.return_value={"timestamp":[latest['timestamp'][0]+300],
                "open":[100],"high":[120],"low":[100],"close":[115],"strike":[1000]}
        client.history_events=[{"reason":"request_error"}] if missing else []
        client.history_coverage=[]
        if partial:
            def fetch_partial(*args, **kwargs):
                client.history_events.append({"reason":"partial_session_tail"})
                return previous
            client.fetch_intraday_candles.side_effect=fetch_partial
        with patch.object(bot,"datetime",End),patch.object(bot,"DhanStockOptionsClient",return_value=client), \
             patch.object(bot.DynamicStockOptionsUniverse,"load_universe",return_value={"1":{"symbol":"TEST","eq_security_id":"1","fno_lot_size":100}}), \
             patch.object(bot,"evaluate_stock_setup",return_value={"opt_type":"CE","scanner":"TEST"}),patch.object(bot,"atomic_write_json"):
            ledger=bot.run_stock_options_historical_backtest(5,0,bot.DynamicStrategyConfig({},argparse.Namespace()),MagicMock())
            if partial:
                client.fetch_rolling_stock_option.assert_called_once()
            return ledger

    def test_current_replay_matches_five_day_live_history(self):
        class End(datetime):
            @classmethod
            def now(cls,tz=None):return cls(2026,9,19,tzinfo=tz)
        data={k:[] for k in ('timestamp','open','high','low','close','volume')}
        for day in range(8,19):
            price=103 if day==18 else 100
            values=(datetime(2026,9,day,10,tzinfo=IST).timestamp(),price,price+1,price-1,price,1000)
            for k,v in zip(data,values):data[k].append(v)
        client=MagicMock();client.fetch_intraday_candles.return_value=data
        client.history_events=[];client.history_coverage=[];observed=[]
        def evaluate(prefix,as_of,*args,**kwargs):
            observed.append((prefix,as_of));return None
        with patch.object(bot,'datetime',End),patch.object(bot,'DhanStockOptionsClient',return_value=client), \
             patch.object(bot.DynamicStockOptionsUniverse,'load_universe',return_value={'1':{'symbol':'TEST','eq_security_id':'1','fno_lot_size':100}}), \
             patch.object(bot,'evaluate_stock_setup',side_effect=evaluate),patch.object(bot,'atomic_write_json'):
            result=bot.run_stock_options_historical_backtest(5,0,bot.DynamicStrategyConfig({},argparse.Namespace()),MagicMock(),current_implementation=True)
        self.assertEqual(client.fetch_intraday_candles.call_args.kwargs['from_date'],'2026-09-09')
        self.assertEqual(result['entry_configuration']['signal_lookback_calendar_days'],5)
        self.assertEqual(len(observed),1)
        prefix,as_of=observed[0]
        self.assertEqual(datetime.fromtimestamp(prefix['timestamp'][0],IST).day,13)
        self.assertTrue(all(t+300<=as_of for t in prefix['timestamp']))

    def test_scored_five_minute_rolling_prices_require_contract_verification(self):
        result=self.run_case(False,scored=True)
        self.assertEqual(result['total_trades'],1)
        self.assertTrue(result['data_coverage_complete'])
        self.assertFalse(result['price_identity_validated'])
        self.assertEqual(result['status'],'INCOMPLETE_DATA')
        self.assertEqual(result['trades'][0]['data_source'],'ROLLING_ATM_5M_UNVERIFIED_CONTRACT_IDENTITY')

    def test_history_warning_allows_setups_but_marks_report_incomplete(self):
        ledger=self.run_case(False,partial=True)
        self.assertEqual(ledger["status"],"INCOMPLETE_DATA")
        self.assertEqual(ledger["stock_coverage"],1)
        self.assertEqual(ledger["total_trades"],0)

    def test_missing_options_cannot_be_a_clean_zero_trade_result(self):
        ledger=self.run_case(True)
        self.assertEqual(ledger["status"],"INCOMPLETE_DATA")
        self.assertFalse(ledger["data_coverage_complete"])
        self.assertEqual(ledger["data_rejection_counts"],{"option_history_unavailable":1})
        self.assertEqual(ledger["total_trades"],0)

    def test_premium_rejection_is_not_missing_data(self):
        ledger=self.run_case(False)
        self.assertEqual(ledger["status"],"COMPLETED_EXIT_REPLAY")
        self.assertEqual(ledger["strategy_rejection_counts"],{"premium_outside_limits":1})
        self.assertFalse(ledger["full_strategy_validated"])


if __name__=="__main__":unittest.main()
