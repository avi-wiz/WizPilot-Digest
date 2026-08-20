"""Minimal Redash API client — stdlib only (works on Python 3.9).

Two ways to get data out of Redash:

  1. results_csv()  — GET the LAST CACHED result of a saved query. One call, no
     polling, but returns whatever Redash last ran, which may be stale and uses
     whatever parameter values were saved with it.

  2. refresh()      — POST a new job with your own parameters, poll until it
     finishes, then fetch the fresh result. This is what a daily digest needs,
     because the date parameter changes every run.

Auth: a per-user API key ("API Key" in your Redash profile) OR a per-query key.
A user key can run any query you can see; a query key is scoped to one query.
"""
from __future__ import annotations
import csv
import io
import json
import time
import urllib.error
import urllib.parse
import urllib.request


class RedashError(RuntimeError):
    pass


class Redash:
    def __init__(self, base_url: str, api_key: str, timeout: int = 30):
        self.base = base_url.rstrip("/")
        self.key = api_key
        self.timeout = timeout

    def _req(self, method: str, path: str, body: dict | None = None) -> dict:
        url = f"{self.base}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Key {self.key}")
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:400]
            raise RedashError(f"{method} {path} -> HTTP {e.code}: {detail}") from None
        except urllib.error.URLError as e:
            raise RedashError(f"{method} {path} -> cannot reach Redash: {e.reason}") from None
        return json.loads(raw) if raw else {}

    # --- introspection -----------------------------------------------------
    def whoami(self) -> dict:
        return self._req("GET", "/api/session")

    def get_query(self, qid: int) -> dict:
        return self._req("GET", f"/api/queries/{qid}")

    # --- fetching ----------------------------------------------------------
    def results_csv(self, qid: int) -> list[dict]:
        """Last cached result as list-of-dicts. No polling, possibly stale."""
        url = f"{self.base}/api/queries/{qid}/results.csv"
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Key {self.key}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                text = r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            raise RedashError(f"results.csv({qid}) -> HTTP {e.code}") from None
        return list(csv.DictReader(io.StringIO(text)))

    def refresh(self, qid: int, params: dict | None = None,
                poll_every: float = 2.0, max_wait: float = 300.0) -> list[dict]:
        """Run the query fresh with `params`, wait for it, return rows."""
        body = {"parameters": params or {}, "max_age": 0}
        job = self._req("POST", f"/api/queries/{qid}/results", body).get("job")
        if not job:
            raise RedashError("Redash did not return a job — check query id/permissions.")

        job_id, waited = job["id"], 0.0
        while waited < max_wait:
            j = self._req("GET", f"/api/jobs/{job_id}").get("job", {})
            status = j.get("status")
            if status == 3:                      # SUCCESS
                rid = j.get("query_result_id")
                res = self._req("GET", f"/api/query_results/{rid}")
                return res["query_result"]["data"]["rows"]
            if status == 4:                      # FAILURE
                raise RedashError(f"Query {qid} failed in Redash: {j.get('error')}")
            time.sleep(poll_every)
            waited += poll_every
        raise RedashError(f"Query {qid} still running after {max_wait}s")
