"""The judge: a strong model judges every article as teaching material for one
student. One call scores a numbered batch; a second, stronger model re-reads close
calls with the full text before anything is cut. All model access lives here.

Two ways to run the first pass. Synchronous: the run waits for every call. Batch
(the default): a run submits what is pending to the Message Batches API at half
price and records the judgments on the next run, half an hour later. Reviews
travel the same way in batch mode, so a close call waits one run before it can be cut.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import store
from .config import Config
from .ids import resolve_ordinal
from .judgment import (  # noqa: F401 — re-exported for one release: these lived here until October 2026
    FLAG_ALIASES,
    FLAG_CODES,
    FLAGS,
    KIND_CODES,
    KINDS,
    PLAIN_JSON_INSTRUCTION,
    SUBSCORES,
    DupIndex,
    JudgmentBatchOut,
    JudgmentIn,
    JudgmentOut,
    Verdict,
    canonical_url,
    compose,
    find_duplicate,
    matches_topic,
    normalize_flags,
    normalized_title,
    parse_judgments,
)
from .llm import (
    LLM,
    BatchNotFound,
    BudgetExceeded,
    JudgeError,
    ModelConfigError,
    NoJudgments,
)

log = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

BATCH_WAIT_MINUTES = 60  # a half-empty batch is still submitted once its oldest article has waited this long
BATCH_LOST_HOURS = 26  # the API expires a batch after 24h; one never seen to end by then is given up on


def load_prompt(name: str, root: Path | None = None) -> str:
    """A prompt by name. A copy in <root>/prompts/ overrides the packaged one, so
    prompts can be edited next to config.yaml without touching the package."""
    candidates = [PROMPTS_DIR / f"{name}.md"]
    if root is not None:
        candidates.insert(0, root / "prompts" / f"{name}.md")
    for path in candidates:
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    raise JudgeError(f"prompt {name!r} not found in {[str(c) for c in candidates]}")


def fill_prompt(text: str, cfg: Config) -> str:
    """The prompt's placeholders: `<<home_flag>>` names the student's home ground when one is
    configured (the model then writes the `home` flag) and disappears when none is."""
    home = cfg.judge.home_angle.strip()
    home_flag = f"home (an angle on the student's home ground, {home}), " if home else ""
    return text.replace("<<home_flag>>", home_flag)


def _fmt_date(published: int | None) -> str:
    if not published:
        return "unknown date"
    try:
        return datetime.fromtimestamp(int(published), tz=timezone.utc).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError, TypeError):
        return "unknown date"  # a feed's bad timestamp must not fail its batch every run for two weeks


def article_block(n: int, row: sqlite3.Row | dict, chars: int) -> str:
    text = (row["text"] or "").strip()
    snippet = text[:chars] + (" […]" if len(text) > chars else "")
    host = urlsplit(row["url"] or "").netloc.lower().removeprefix("www.") if row["url"] else ""
    return (
        f"[{n}]\n"
        f"feed: {row['feed_title'] or '?'} · folder: {row['folder'] or 'none'}\n"
        f"title: {row['title'] or '(untitled)'}\n"
        f"author: {row['author'] or 'unknown'} · published: {_fmt_date(row['published'])} · site: {host}\n"
        f"text: {snippet or '(no text in the feed)'}\n"
    )


def has_more_to_read(row: sqlite3.Row | dict, cfg: Config) -> bool:
    """A review is worth paying for only when the full text holds more than the
    opening the first pass already saw."""
    has_len = "text_len" in row.keys()  # noqa: SIM118 — `in row` on a sqlite3.Row tests the values, not the keys
    length = row["text_len"] if has_len else len(row["text"] or "")
    return int(length or 0) > cfg.judge.text_chars + 200


class _ReviewFeeder:
    """Keeps at most ``workers`` reviews running and nothing queued behind them, so a budget
    or a deadline stops the submissions, never a review already paid for."""

    def __init__(
        self,
        judge: Judge,
        pool: ThreadPoolExecutor,
        todo: list[sqlite3.Row],
        run_id: int | None,
        workers: int,
        stop_after: float | None,
    ) -> None:
        self.judge = judge
        self.pool = pool
        self.queue = list(todo)
        self.run_id = run_id
        self.workers = workers
        self.stop_after = stop_after
        self.futures: dict[Any, sqlite3.Row] = {}
        self.running: set[Any] = set()
        self.budget_hit = False

    def feed(self) -> None:
        while self.queue and len(self.running) < self.workers:
            if self.judge.llm.budget_left() <= 0:
                self.budget_hit = True
                self.queue.clear()
                return
            if self.stop_after is not None and time.monotonic() > self.stop_after:
                self.queue.clear()
                return
            row = self.queue.pop(0)
            full = store.article(self.judge.conn, row["id"])
            if full is None:
                continue
            fut = self.pool.submit(self.judge.review, full, self.run_id)
            self.futures[fut] = row
            self.running.add(fut)


class Judge:
    def __init__(
        self,
        cfg: Config,
        llm: LLM,
        conn: sqlite3.Connection,
        context: str = "",
        expansion_topics: list[str] | None = None,
        learned_text: str | None = None,
    ) -> None:
        """``learned_text`` stands in for profile/learned.md (the weekly rewrite's gate judges
        with a proposed model without writing it to disk)."""
        self.cfg = cfg
        self.llm = llm
        self.conn = conn
        self.prompt_version = cfg.judge.prompt_version
        self.expansion_topics = expansion_topics or []
        base = fill_prompt(load_prompt(self.prompt_version, cfg.root), cfg)
        intro, _, rubric = base.partition("\n\n")
        cfg.check_about()  # the backstop; run and catch-up refuse earlier, before any network
        about = cfg.read_about() or "(nothing written about the student yet)"
        learned = (cfg.read_learned() if learned_text is None else learned_text) or "(no learned profile yet)"
        # the cached prefix changes only when the student's description or the learned profile
        # does; the concept lists change with every star and read, so they travel in the user
        # message instead
        student = f"# About the student\n{about}\n\n# Learned profile\n{learned}"
        self.system_text = f"{intro}\n\n{rubric}\n\n{student}"
        self.second_look_text = f"{load_prompt('review.v1', cfg.root)}\n\n{rubric}\n\n{student}"
        self.context = context or "(no reading history yet)"
        self.batch_mode = cfg.models.batch  # a run may switch this off (`bodh run --sync`)
        self.rejudge = self.rejudge_rule(cfg)  # None when a quick judge must not touch the judge's work
        self.stale_tags: dict[str, list[str]] = {}  # band tag -> articles a re-score moved out of it

    def _user_text(self, rows: list, chars: int) -> str:
        blocks = "\n".join(article_block(i, r, chars) for i, r in enumerate(rows, 1))
        return f"# Concepts\n{self.context}\n\n# Articles\n{blocks}"

    # -- what is pending --------------------------------------------------------

    def pending_rows(
        self, limit: int | None = None, exclude_inflight: bool = False
    ) -> tuple[list[sqlite3.Row], int]:
        """Unread articles still to judge, newest first, with duplicates of earlier stories
        recorded on the spot (no model call). Returns (rows to judge, duplicates recorded)."""
        rows = store.unscored_unread(
            self.conn,
            self.prompt_version,
            self.cfg.limits.max_age_days,
            self.cfg.unjudged_folders(),
            exclude_inflight=exclude_inflight,
            limit=limit,
            rejudge=self.rejudge,
        )
        todo: list[sqlite3.Row] = []
        dupes = 0
        index = DupIndex(self.conn)
        for row in rows:
            dup = index.find(row)
            if dup is not None:
                self._record_duplicate(row, dup)
                dupes += 1
            else:
                todo.append(row)
        return todo, dupes

    def pending_count(self, exclude_inflight: bool = False) -> int:
        return store.unscored_unread_count(
            self.conn,
            self.prompt_version,
            self.cfg.limits.max_age_days,
            self.cfg.unjudged_folders(),
            exclude_inflight=exclude_inflight,
            rejudge=self.rejudge,
        )

    @staticmethod
    def rejudge_rule(cfg: Config) -> tuple[str, float] | None:
        """(judge model, min score): what another model judged at or above the score is
        judged again by the judge, so the keepers rest on the judge's opinion."""
        if cfg.catch_up.rejudge_at is None:
            return None
        return cfg.models.judge, float(cfg.catch_up.rejudge_at)

    # -- first pass, synchronous ----------------------------------------------

    def judge_batch(self, rows: list[sqlite3.Row], run_id: int | None) -> dict[str, Verdict]:
        ordered = [r["id"] for r in rows]
        user_text = self._user_text(rows, self.cfg.judge.text_chars)
        verdicts = self.llm.judge(self.cfg.models.judge, self.system_text, user_text, run_id, "teacher")
        return self._by_article(verdicts, ordered)

    @staticmethod
    def _by_article(verdicts: list[Verdict], ordered: list[str]) -> dict[str, Verdict]:
        """Verdicts keyed by article id, in batch order. A duplicate may point only at an
        earlier article of the batch that is not itself a duplicate: a forward pointer or a
        mutual pair would cap both copies of a good story at 20, and the later copy then has
        no original to be a duplicate of. Otherwise the pointer and the flag are dropped."""
        out: dict[str, Verdict] = {}
        dupes: set[str] = set()
        for v in sorted(verdicts, key=lambda v: v.n):
            aid = resolve_ordinal(v.n, ordered)
            if aid is None or aid in out:
                continue
            if v.dupe_of is not None or "duplicate" in v.flags:
                target = resolve_ordinal(v.dupe_of, ordered) if v.dupe_of is not None else None
                earlier = target is not None and target != aid and ordered.index(target) < ordered.index(aid)
                if earlier and target not in dupes:
                    verdict = v.model_copy(update={"dupe_of": target})
                    dupes.add(aid)
                else:
                    verdict = v.model_copy(
                        update={"dupe_of": None, "flags": [f for f in v.flags if f != "duplicate"]}
                    )
            else:
                verdict = v
            out[aid] = verdict
        return out

    def score_pending(
        self,
        run_id: int | None,
        limit: int | None = None,
        workers: int = 3,
        on_progress: Any = None,
        stop_after: float | None = None,
    ) -> dict:
        """Score every pending article now, waiting for each call. Resumable: anything not
        scored stays pending for the next run. ``on_progress(stats)`` is called after every
        batch that lands; past ``stop_after`` (a time.monotonic() value) no new call starts.
        Returns stats."""
        stats = {
            "scored": 0,
            "dupes": 0,
            "failed_batches": 0,
            "empty_batches": 0,
            "budget_stop": False,
            "pending_after": 0,
            "total": 0,
            "stopped_early": False,
        }
        todo, stats["dupes"] = self.pending_rows(limit, exclude_inflight=True)  # a batch may hold some
        stats["total"] = len(todo)
        size = self.cfg.judge.articles_per_call
        batches = [todo[i : i + size] for i in range(0, len(todo), size)]
        workers = max(1, workers)
        pool = ThreadPoolExecutor(max_workers=workers)
        try:
            futures = {}
            queue = list(enumerate(batches))
            running: set = set()

            def feed() -> None:
                # at most one call per worker in flight and nothing queued behind it, so the
                # deadline and the budget stop new calls at once, not after a backlog drains
                while queue and len(running) < workers:
                    if self.llm.budget_left() <= 0:
                        stats["budget_stop"] = True
                        queue.clear()
                        return
                    if stop_after is not None and time.monotonic() > stop_after:
                        stats["stopped_early"] = True
                        queue.clear()
                        return
                    bi, batch = queue.pop(0)
                    fut = pool.submit(self.judge_batch, batch, run_id)
                    futures[fut] = bi
                    running.add(fut)

            feed()
            while running:
                done = next(as_completed(list(running)))
                running.discard(done)
                self._absorb_future(done, batches[futures[done]], stats, on_progress, len(todo))
                feed()  # after the absorb: a rejection or an interrupt must not let one more call out
        finally:
            # a rejected key or model id, a timeout, Ctrl-C: whatever is queued is never sent
            pool.shutdown(wait=True, cancel_futures=True)
        stats["pending_after"] = self.pending_count(exclude_inflight=True)
        return stats

    def _absorb_future(self, future: Any, batch: list, stats: dict, on_progress: Any, total: int) -> None:
        try:
            verdicts = future.result()
        except BudgetExceeded:
            stats["budget_stop"] = True
            return
        except ModelConfigError:
            raise
        except NoJudgments as exc:  # the model answered with nothing: counted against the articles
            stats["failed_batches"] += 1
            stats["empty_batches"] += 1
            store.bump_judge_failures(self.conn, [r["id"] for r in batch])
            log.warning("judge: batch of %d: %s; they stay pending", len(batch), exc)
            return
        except Exception:  # one call failed (network, a 5xx after the retries): its articles stay pending
            stats["failed_batches"] += 1
            log.exception("judge: batch of %d failed; they stay pending", len(batch))
            return
        by_id = {r["id"]: r for r in batch}
        for aid, v in verdicts.items():
            self._record(by_id[aid], v)
            stats["scored"] += 1
        if not verdicts and batch:
            stats["empty_batches"] += 1
            store.bump_judge_failures(self.conn, [r["id"] for r in batch])
            log.warning("judge: a batch of %d came back with no judgment", len(batch))
        log.info("judge: judged %d of %d", stats["scored"], total)
        if on_progress is not None:
            on_progress(stats)

    # -- first pass, batched --------------------------------------------------

    def submit_pending(self, run_id: int | None, limit: int | None = None) -> dict:
        """Send what is pending to the Batches API. A batch smaller than one call's worth
        waits for more articles unless its oldest has waited an hour. Returns stats."""
        stats = {
            "submitted": 0,
            "requests": 0,
            "dupes": 0,
            "budget_stop": False,
            "batch_id": None,
            "waiting": 0,
        }
        limit = limit or self.cfg.limits.per_run_batch
        todo, stats["dupes"] = self.pending_rows(limit, exclude_inflight=True)
        if not todo:
            return stats
        size = self.cfg.judge.articles_per_call
        if len(todo) < size and not self._waited_long_enough(todo):
            stats["waiting"] = len(todo)
            return stats
        est = max(self.cfg.models.est_usd_per_article, 1e-6)
        allowed = int(max(0.0, self.llm.budget_left()) / est)
        if allowed < len(todo):
            stats["budget_stop"] = True
            todo = todo[:allowed]
            if not todo:
                return stats
        model = self.cfg.models.judge
        ttl = self.cfg.models.cache_ttl
        requests, members = [], []
        for bi in range(0, len(todo), size):
            batch = todo[bi : bi + size]
            custom_id = f"t{run_id or 0}-{bi // size}"
            params = self.llm.request_params(
                model, self.system_text, self._user_text(batch, self.cfg.judge.text_chars), ttl
            )
            requests.append({"custom_id": custom_id, "params": params})
            members.append((custom_id, [r["id"] for r in batch]))
        batch_id = self.llm.submit_batch(requests)
        store.add_batch(self.conn, batch_id, run_id, "teacher", model, members)
        stats.update(submitted=len(todo), requests=len(requests), batch_id=batch_id)
        log.info("judge: submitted batch %s: %d articles in %d requests", batch_id, len(todo), len(requests))
        return stats

    @staticmethod
    def _waited_long_enough(rows: list[sqlite3.Row]) -> bool:
        oldest = min((r["fetched_at"] or "") for r in rows)
        if not oldest:
            return True
        try:
            seen = datetime.fromisoformat(oldest)
        except ValueError:
            return True
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
        return store.utcnow() - seen >= timedelta(minutes=BATCH_WAIT_MINUTES)

    def submit_reviews(self, run_id: int | None, rows: list[sqlite3.Row]) -> int:
        """Close calls go to the stronger model with the full text, one request each."""
        rows = [r for r in rows if r["id"] not in store.inflight_ids(self.conn, "second_look")]
        if not rows:
            return 0
        model = self.cfg.models.reviewer
        requests, members = [], []
        for n, row in enumerate(rows):
            full = store.article(self.conn, row["id"])
            if full is None:
                continue
            custom_id = f"s{run_id or 0}-{n}"
            user_text = self._user_text([dict(full)], self.cfg.review.chars)
            params = self.llm.request_params(
                model, self.second_look_text, user_text, self.cfg.models.cache_ttl
            )
            requests.append({"custom_id": custom_id, "params": params})
            members.append((custom_id, [row["id"]]))
        if not requests:
            return 0
        batch_id = self.llm.submit_batch(requests)
        store.add_batch(self.conn, batch_id, run_id, "second_look", model, members)
        log.info("judge: submitted %d reviews as batch %s", len(requests), batch_id)
        return len(requests)

    def collect(self, run_id: int | None, client: Any = None, on_progress: Any = None) -> dict:
        """Record the judgments of every finished batch. Articles whose request failed go
        back to pending (first pass) or count as examined and stay unread (review)."""
        stats = {"scored": 0, "second_looks": 0, "saved": 0, "failed": 0, "empty": 0, "open": 0, "notes": []}
        for b in store.open_batches(self.conn):
            results = self._finished_results(b, stats)
            if results is None:
                continue
            self._absorb(b, results, run_id, client, stats)
            if on_progress is not None:
                on_progress(stats)
        return stats

    def _finished_results(self, b: sqlite3.Row, stats: dict) -> list[Any] | None:
        """The results of one batch when it has ended and they can be fetched; None when it is
        still open (counted) or has been given up on (closed as lost). A rejected key or model
        is raised: nothing else can go right after it."""
        try:
            status = self.llm.batch_status(b["id"])
        except BatchNotFound as exc:
            self._give_up(b, f"vanished from the API ({exc})", stats)
            return None
        except ModelConfigError:
            raise
        except Exception:  # the API or the network; the batch is polled again next run
            log.exception("judge: could not poll batch %s", b["id"])
            stats["open"] += 1
            return None
        if status != "ended":
            if self._lost(b):
                self._give_up(b, f"still {status} after {BATCH_LOST_HOURS}h", stats)
            else:
                stats["open"] += 1
            return None
        try:
            return self.llm.batch_results(b["id"])
        except BatchNotFound as exc:
            self._give_up(b, f"vanished from the API ({exc})", stats)
        except ModelConfigError:
            raise
        except Exception:  # the results stream failed; tried again next run, given up after the deadline
            log.exception("judge: could not fetch results of batch %s", b["id"])
            if self._lost(b):  # ended, but its results will not come: the same deadline applies
                self._give_up(b, f"ended but its results could not be fetched for {BATCH_LOST_HOURS}h", stats)
            else:
                stats["open"] += 1
        return None

    def _give_up(self, b: sqlite3.Row, why: str, stats: dict) -> None:
        """Close a batch whose results will never come; its articles are pending again (first
        pass) or count as examined and stay unread (reviews)."""
        if b["purpose"] == "second_look":
            for ids in store.batch_members(self.conn, b["id"]).values():
                store.record_review_failure(self.conn, ids[0])
                stats["failed"] += 1
        store.close_batch(self.conn, b["id"], "lost", why)
        log.warning("judge: batch %s given up on: %s", b["id"], why)
        stats["notes"].append(f"batch {b['id'][:12]} given up on ({why}); its articles are pending again")

    @staticmethod
    def _lost(b: sqlite3.Row) -> bool:
        try:
            created = datetime.fromisoformat(b["created_at"])
        except (TypeError, ValueError):
            return False
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return store.utcnow() - created > timedelta(hours=BATCH_LOST_HOURS)

    def _absorb(
        self, b: sqlite3.Row, results: list[Any], run_id: int | None, client: Any, stats: dict
    ) -> None:
        """Record one finished batch: judgments, reviews, costs and the batch's closing in
        one transaction, so an interrupt leaves it open and untouched for the next run to
        absorb again rather than half-recorded with its costs counted twice."""
        with store.transaction(self.conn):
            model, first_error = self._absorb_results(b, results, run_id, client, stats)
        if first_error:
            # after the commit: the LLM remembers a rejected request shape on its own connection,
            # and that write would wait on this one's open transaction until it timed out
            self.llm.adapt_to_error(model, first_error)

    def _absorb_results(
        self, b: sqlite3.Row, results: list[Any], run_id: int | None, client: Any, stats: dict
    ) -> tuple[str, str]:
        """Returns (model, first error message) for the caller to act on once the transaction is over."""
        members = store.batch_members(self.conn, b["id"])
        purpose, model = b["purpose"], b["model"]
        errored, empty, first_error, answered = 0, 0, "", set()
        for res in results:
            ids = members.get(res.custom_id)
            if not ids or res.custom_id in answered:  # unknown, or listed twice: once is enough
                continue
            answered.add(res.custom_id)
            result = res.result
            if getattr(result, "type", "") != "succeeded":
                errored += 1
                err = getattr(getattr(result, "error", None), "error", None)
                first_error = (
                    first_error or f"{getattr(result, 'type', '?')}: {getattr(err, 'message', '') or ''}"
                )
                if purpose == "second_look":
                    store.record_review_failure(self.conn, ids[0])
                    stats["failed"] += 1
                continue
            message = result.message
            usage, usd = self.llm.cost(model, message.usage, self.cfg.models.batch_discount)
            store.record_cost(self.conn, run_id, model, purpose, usage, usd)
            verdicts = parse_judgments(self.llm.message_text(message))
            if purpose == "teacher":
                done = self._absorb_judge(ids, verdicts or [])
                stats["scored"] += done
                if not done:
                    empty += 1
                    store.bump_judge_failures(self.conn, ids)
            else:
                self._absorb_review(ids[0], verdicts, client, stats)
        for custom_id, ids in members.items():
            if custom_id not in answered and purpose == "second_look":
                store.record_review_failure(self.conn, ids[0])
                stats["failed"] += 1
        notes = []
        if errored:
            notes.append(f"{errored} of {b['n_requests']} requests failed: {first_error}")
        if empty:
            stats["empty"] += empty
            notes.append(
                f"{empty} of {b['n_requests']} requests came back with no judgment "
                f"(an article is left alone after {store.MAX_JUDGE_FAILURES}; `bodh safety` shows how many)"
            )
        note = "; ".join(notes)
        store.close_batch(self.conn, b["id"], "partial" if errored or empty else "done", note)
        if note:
            log.warning("judge: batch %s: %s", b["id"], note)
            stats["notes"].append(f"batch {b['id'][:12]}: {note}")
        return model, first_error if errored else ""

    def _absorb_judge(self, ids: list[str], verdicts: list[Verdict]) -> int:
        by_id = {
            r["id"]: r
            for r in self.conn.execute(
                f"SELECT * FROM articles WHERE id IN ({','.join('?' * len(ids))})", ids
            )
        }
        done = 0
        for aid, v in self._by_article(verdicts, ids).items():
            row = by_id.get(aid)
            if row is None:
                continue
            self._record(row, v)
            done += 1
        return done

    def _absorb_review(self, aid: str, verdicts: list[Verdict] | None, client: Any, stats: dict) -> None:
        current = self.conn.execute(
            "SELECT s.score, a.folder FROM scores s JOIN articles a ON a.id = s.article_id WHERE s.article_id = ?",
            (aid,),
        ).fetchone()
        if current is None or not verdicts:
            store.record_review_failure(self.conn, aid)
            stats["failed"] += 1
            return
        second = compose(verdicts[0].model_copy(update={"dupe_of": None}), self.cfg, self.expansion_topics)
        stats["second_looks"] += 1
        if self.apply_review(aid, float(current["score"]), second, current["folder"], client):
            stats["saved"] += 1

    def apply_review(
        self, aid: str, first: float, second: float, folder: str | None, client: Any = None
    ) -> bool:
        """Keep the higher score, retag the band if it moved. True when the article now
        clears its cut-off."""
        new_score = max(first, second)
        new_bucket = self.cfg.tags.band_for(new_score)
        old_bucket = self.cfg.tags.band_for(first)
        store.record_review(self.conn, aid, second, self.cfg.models.reviewer, new_score, new_bucket)
        if new_bucket != old_bucket and client is not None:
            try:
                client.untag([aid], old_bucket)
            except Exception:  # cosmetic: a stale band tag; the next tagging pass sets the right one
                log.warning("cut: could not remove old band tag %s from %s", old_bucket, aid)
        floor = self.cfg.cutoff_for(folder)
        return floor is None or new_score >= floor

    # -- recording --------------------------------------------------------------

    def _record(self, row: sqlite3.Row, v: Verdict) -> None:
        d = v.model_dump()
        d["concept"] = (d.get("concept") or "").strip().lower()[:60]
        d["lesson"] = (d.get("lesson") or "").strip()[:200]
        d["dupe_of"] = v.dupe_of if isinstance(v.dupe_of, str) else None
        score = compose(d, self.cfg, self.expansion_topics)
        stale = store.record_score(
            self.conn,
            row["id"],
            d,
            base_score=score,
            score=score,
            bucket=self.cfg.tags.band_for(score),
            prompt_version=self.prompt_version,
            model=self.cfg.models.judge,
        )
        if stale:
            self.stale_tags.setdefault(stale, []).append(row["id"])

    def _record_duplicate(self, row: sqlite3.Row, original: dict) -> None:
        d = {
            "quality": 20,
            "learning": 10,
            "fit": 20,
            "breadth": 10,
            "relevance": 20,
            "lesson": f"Same story as: {original['title'][:80]}",
            "concept": original["concept"] or "duplicate",
            "kind": "other",
            "flags": ["duplicate"],
            "dupe_of": original["id"],
            "confidence": "high",
        }
        stale = store.record_score(
            self.conn,
            row["id"],
            d,
            base_score=20.0,
            score=20.0,
            bucket=self.cfg.tags.band_for(20.0),
            prompt_version=self.prompt_version,
            model="dedup",
        )
        if stale:
            self.stale_tags.setdefault(stale, []).append(row["id"])

    # -- review, synchronous -------------------------------------------------

    def reviews(
        self,
        rows: list[sqlite3.Row],
        run_id: int | None,
        client: Any = None,
        workers: int | None = None,
        stop_after: float | None = None,
        max_looks: int | None = None,
    ) -> dict[str, bool | None]:
        """Several reviews at once, each recorded the moment it lands (a look that was
        paid for is never lost to a timeout or a Ctrl-C later in the run). ``rows`` are
        autocut candidates with their score and folder. Returns {article id: True when the
        article now clears its cut-off, False when the cut stands, None when the look failed
        (recorded as failed; the article stays unread)}. An article missing from the result
        was never examined (the budget, the deadline, or the per-run cap): it stays unread."""
        results: dict[str, bool | None] = {}
        cap = self.cfg.review.max_per_run if max_looks is None else max_looks
        todo = [r for r in rows[:cap] if store.article(self.conn, r["id"]) is not None]
        if len(rows) > cap:
            log.info("judge: %d close calls; looking at %d this run", len(rows), cap)
        workers = max(1, workers or self.cfg.limits.parallel_calls)
        pool = ThreadPoolExecutor(max_workers=workers)
        feeder = _ReviewFeeder(self, pool, todo, run_id, workers, stop_after)
        try:
            feeder.feed()
            while feeder.running:
                done = next(as_completed(list(feeder.running)))
                feeder.running.discard(done)
                row = feeder.futures[done]
                self._absorb_review_result(done, row, client, results, feeder)
                feeder.feed()
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
        if feeder.budget_hit:
            log.warning("judge: budget reached during reviews; unexamined close calls stay unread")
        return results

    def _absorb_review_result(
        self, done: Any, row: sqlite3.Row, client: Any, results: dict[str, bool | None], feeder: _ReviewFeeder
    ) -> None:
        """One finished review: recorded at once as a save, a confirmed cut or a failure. An
        article the budget ran out on stays unexamined (absent from the results)."""
        aid = row["id"]
        try:
            second = done.result()
        except BudgetExceeded:
            feeder.budget_hit = True
            return  # unexamined: the budget ran out while it waited
        except ModelConfigError:
            raise
        except (
            Exception
        ):  # one review failed; recorded so it is not retried every run, the article stays unread
            log.exception("judge: review failed for %s", aid)
            second = None
        if second is None:
            store.record_review_failure(self.conn, aid)
            results[aid] = None
        else:
            results[aid] = self.apply_review(aid, float(row["score"]), float(second), row["folder"], client)

    def review(self, row: sqlite3.Row, run_id: int | None) -> float | None:
        """Re-read one article with the full text on the stronger model, now. Returns the
        composed score of that judgment, or None if it failed. The caller keeps the max."""
        full = dict(row)
        user_text = self._user_text([full], self.cfg.review.chars)
        try:
            verdicts = self.llm.judge(
                self.cfg.models.reviewer, self.second_look_text, user_text, run_id, "second_look"
            )
        except (BudgetExceeded, ModelConfigError):
            raise
        except Exception:  # the review could not be had: recorded as failed, the article stays unread
            log.exception("judge: review failed for %s", row["id"])
            return None
        if not verdicts:
            return None
        v = verdicts[0].model_copy(update={"dupe_of": None})
        return compose(v, self.cfg, self.expansion_topics)


def concept_context(conn: sqlite3.Connection, cfg: Config) -> str:
    """The lists for the judge: what the student loved, read, skipped, and where the level
    missed, by concept, from stars, reads and the student's verdict tags."""
    days = cfg.profile.concept_window_days
    since = store.days_ago_iso(days)
    loved = store.concept_counts(conn, store.LOVED_KINDS, since)
    read = store.concept_counts(conn, ["good", "read", "rescue"], since)
    neg = store.concept_counts(conn, store.NEGATIVE_KINDS, since)
    basic = store.concept_counts(conn, ["basic"], since, limit=20)
    heavy = store.concept_counts(conn, ["heavy"], since, limit=20)
    fmt = lambda pairs: ", ".join(f"{c}: {n}" for c, n in pairs)  # noqa: E731
    lines = []
    if loved:
        lines.append(
            f"Loved (starred or marked great) in the last {days} days (concept: count): {fmt(loved)}"
        )
    if read:
        lines.append(f"Read, marked good or rescued in the last {days} days: {fmt(read)}")
    if not loved and not read:
        lines.append(f"Read in the last {days} days: nothing recorded yet.")
    if neg:
        lines.append(f"Marked bad or meh, or dismissed: {fmt(neg)}")
    if basic:
        lines.append(f"Too basic for the student (score fit low for introductions to these): {fmt(basic)}")
    if heavy:
        lines.append(f"Too dense for the student (prefer gentler entries to these): {fmt(heavy)}")
    return "\n".join(lines)
