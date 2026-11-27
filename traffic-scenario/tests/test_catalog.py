import sqlite3

import pytest

from backend import catalog


# 정상 종료 시 저장과 연결 종료 확인
def test_connection_commits_and_closes(tmp_path):
    path = tmp_path / "catalog.sqlite"

    with catalog.connect(path) as con:
        con.execute("CREATE TABLE sample(value INTEGER)")
        con.execute("INSERT INTO sample VALUES(1)")

    with pytest.raises(sqlite3.ProgrammingError):
        con.execute("SELECT 1")

    with catalog.connect(path) as reader:
        assert reader.execute("SELECT value FROM sample").fetchone()[0] == 1


# 오류 발생 시 변경 취소와 연결 종료 확인
def test_connection_rolls_back_and_closes(tmp_path):
    path = tmp_path / "catalog.sqlite"

    with catalog.connect(path) as setup:
        setup.execute("CREATE TABLE sample(value INTEGER)")

    with pytest.raises(ValueError):
        with catalog.connect(path) as con:
            con.execute("INSERT INTO sample VALUES(1)")
            raise ValueError("failed operation")

    with pytest.raises(sqlite3.ProgrammingError):
        con.execute("SELECT 1")

    with catalog.connect(path) as reader:
        assert reader.execute("SELECT COUNT(*) FROM sample").fetchone()[0] == 0
