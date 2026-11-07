"""Reading run logs back, with a clear error for anything unexpected."""

import gzip
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from pydantic import ValidationError

from blackout.logs.schema import (
    LOG_FILE,
    LOG_VERSION,
    RECORD,
    AnyRecord,
    DecisionPointRecord,
    EndRecord,
    HeaderRecord,
    LogFormatError,
    TickRecord,
    UnsupportedLogVersionError,
)
from blackout.scenarios.schema import Scenario


@dataclass(frozen=True, slots=True)
class RunLog:
    path: Path
    header: HeaderRecord
    ticks: tuple[TickRecord, ...]
    decisions: tuple[DecisionPointRecord, ...]
    end: EndRecord | None  # None if the run never finished

    def scenario(self) -> Scenario:
        """The scenario embedded in the header."""
        return Scenario.model_validate(self.header.scenario.spec)


def find_log(path: Path) -> Path:
    """Accept either a run directory or the log file itself."""
    candidate = path / LOG_FILE if path.is_dir() else path
    if not candidate.is_file():
        raise LogFormatError(f"no run log at {candidate}")
    return candidate


@contextmanager
def _open_text(path: Path) -> Iterator[TextIO]:
    """Open a log for reading, decompressing it if it's gzipped."""
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8", newline="\n") as stream:
            yield stream
    else:
        with path.open(encoding="utf-8", newline="\n") as stream:
            yield stream


def read_log(path: Path) -> RunLog:
    """Read and check a whole run log: record order, tick sequence and schema version."""
    log_path = find_log(path)
    header: HeaderRecord | None = None
    ticks: list[TickRecord] = []
    decisions: list[DecisionPointRecord] = []
    end: EndRecord | None = None
    with _open_text(log_path) as stream:
        for number, line in enumerate(stream, start=1):
            where = f"{log_path}:{number}"
            record = _parse(line, where)
            if end is not None:
                raise LogFormatError(f"{where}: a record comes after the end record")
            if isinstance(record, HeaderRecord):
                if header is not None:
                    raise LogFormatError(f"{where}: a second header")
                header = record
            elif header is None:
                raise LogFormatError(f"{where}: the first record must be the header")
            elif isinstance(record, TickRecord):
                if record.t != len(ticks):
                    raise LogFormatError(f"{where}: expected tick {len(ticks)}, found {record.t}")
                ticks.append(record)
            elif isinstance(record, DecisionPointRecord):
                if record.i != len(decisions) or not ticks or record.t != ticks[-1].t:
                    raise LogFormatError(
                        f"{where}: decision point {record.i} at tick {record.t} is out of order"
                    )
                decisions.append(record)
            else:
                if not ticks or record.t != ticks[-1].t:
                    raise LogFormatError(f"{where}: the end record doesn't match the last tick")
                end = record
    if header is None:
        raise LogFormatError(f"{log_path}: the run log is empty")
    return RunLog(log_path, header, tuple(ticks), tuple(decisions), end)


def _parse(line: str, where: str) -> AnyRecord:
    try:
        data = json.loads(line)
    except json.JSONDecodeError as error:
        raise LogFormatError(f"{where}: not valid JSON ({error.msg})") from error
    if not isinstance(data, dict):
        raise LogFormatError(f"{where}: a record must be a JSON object")
    version = data.get("v")
    if version != LOG_VERSION:
        raise UnsupportedLogVersionError(
            f"{where}: run log schema version {version!r} isn't supported;"
            f" this build reads version {LOG_VERSION}"
        )
    try:
        return RECORD.validate_python(data)
    except ValidationError as error:
        first = error.errors()[0]
        kind = data.get("type", "?")
        loc = first["loc"][1:] if first["loc"][:1] == (kind,) else first["loc"]
        place = ".".join(str(part) for part in loc)
        raise LogFormatError(
            f"{where}: invalid {kind!r} record at {place}: {first['msg']}"
        ) from error
