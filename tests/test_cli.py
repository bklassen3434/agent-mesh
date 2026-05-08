from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner
from mesh_cli.main import cli


@pytest.fixture
def runner_with_db(tmp_path: Path) -> tuple[CliRunner, dict[str, str]]:
    db_path = str(tmp_path / "test.db")
    env = {"MESH_DB_PATH": db_path}
    runner = CliRunner()
    # initialize db
    result = runner.invoke(cli, ["init-db"], env=env)
    assert result.exit_code == 0, result.output
    return runner, env


def test_init_db(tmp_path: Path) -> None:
    db_path = str(tmp_path / "new.db")
    runner = CliRunner()
    result = runner.invoke(cli, ["init-db"], env={"MESH_DB_PATH": db_path})
    assert result.exit_code == 0
    assert Path(db_path).exists()


def test_init_db_idempotent(runner_with_db: tuple[CliRunner, dict[str, str]]) -> None:
    runner, env = runner_with_db
    result = runner.invoke(cli, ["init-db"], env=env)
    assert result.exit_code == 0


def test_add_entity_basic(runner_with_db: tuple[CliRunner, dict[str, str]]) -> None:
    runner, env = runner_with_db
    result = runner.invoke(cli, ["add-entity", "--name", "GR00T-N1", "--type", "model"], env=env)
    assert result.exit_code == 0, result.output
    assert "GR00T-N1" in result.output


def test_add_entity_with_aliases(runner_with_db: tuple[CliRunner, dict[str, str]]) -> None:
    runner, env = runner_with_db
    result = runner.invoke(
        cli,
        ["add-entity", "--name", "BERT", "--type", "model",
         "--alias", "bert-base", "--alias", "bert-large"],
        env=env,
    )
    assert result.exit_code == 0


def test_show_entities_empty(runner_with_db: tuple[CliRunner, dict[str, str]]) -> None:
    runner, env = runner_with_db
    result = runner.invoke(cli, ["show-entities"], env=env)
    assert result.exit_code == 0


def test_show_entities_after_add(runner_with_db: tuple[CliRunner, dict[str, str]]) -> None:
    runner, env = runner_with_db
    runner.invoke(cli, ["add-entity", "--name", "Claude-3", "--type", "model"], env=env)
    result = runner.invoke(cli, ["show-entities"], env=env)
    assert result.exit_code == 0
    assert "Claude-3" in result.output


def test_add_source(runner_with_db: tuple[CliRunner, dict[str, str]]) -> None:
    runner, env = runner_with_db
    result = runner.invoke(
        cli,
        ["add-source", "--type", "arxiv", "--url", "https://arxiv.org/abs/2301.00001",
         "--published-at", "2023-01-01T00:00:00"],
        env=env,
    )
    assert result.exit_code == 0, result.output


def test_add_claim(runner_with_db: tuple[CliRunner, dict[str, str]]) -> None:
    runner, env = runner_with_db
    # create entity and source first
    r1 = runner.invoke(cli, ["add-entity", "--name", "GPT-4", "--type", "model"], env=env)
    assert r1.exit_code == 0
    r2 = runner.invoke(
        cli,
        ["add-source", "--type", "arxiv", "--url", "https://arxiv.org/abs/test",
         "--published-at", "2023-01-01T00:00:00"],
        env=env,
    )
    assert r2.exit_code == 0

    # extract IDs from DB
    import duckdb
    conn = duckdb.connect(env["MESH_DB_PATH"])
    entity_row = conn.execute("SELECT id FROM entities LIMIT 1").fetchone()
    source_row = conn.execute("SELECT id FROM sources LIMIT 1").fetchone()
    conn.close()
    assert entity_row is not None and source_row is not None
    eid, sid = entity_row[0], source_row[0]

    result = runner.invoke(
        cli,
        ["add-claim", "--subject", eid, "--predicate", "has_params",
         "--object", '{"value": "1T"}', "--source", sid],
        env=env,
    )
    assert result.exit_code == 0, result.output


def test_add_belief_and_show(runner_with_db: tuple[CliRunner, dict[str, str]]) -> None:
    runner, env = runner_with_db
    result = runner.invoke(
        cli,
        ["add-belief", "--topic", "scaling", "--statement", "Bigger is better.",
         "--confidence", "0.8"],
        env=env,
    )
    assert result.exit_code == 0

    show = runner.invoke(cli, ["show-beliefs"], env=env)
    assert show.exit_code == 0
    assert "scaling" in show.output


def test_add_revision(runner_with_db: tuple[CliRunner, dict[str, str]]) -> None:
    runner, env = runner_with_db
    runner.invoke(
        cli,
        ["add-belief", "--topic", "rl", "--statement", "RL is hard.", "--confidence", "0.7"],
        env=env,
    )

    import duckdb
    conn = duckdb.connect(env["MESH_DB_PATH"])
    belief_row = conn.execute("SELECT id FROM beliefs LIMIT 1").fetchone()
    conn.close()
    assert belief_row is not None
    bid = belief_row[0]

    result = runner.invoke(
        cli,
        ["add-revision", "--belief", bid, "--new-statement", "RL is getting easier.",
         "--new-confidence", "0.6", "--rationale", "new frameworks"],
        env=env,
    )
    assert result.exit_code == 0, result.output


def test_show_revisions(runner_with_db: tuple[CliRunner, dict[str, str]]) -> None:
    runner, env = runner_with_db
    runner.invoke(
        cli,
        ["add-belief", "--topic", "rev-test", "--statement", "Old statement.",
         "--confidence", "0.5"],
        env=env,
    )
    import duckdb
    conn = duckdb.connect(env["MESH_DB_PATH"])
    bid_row = conn.execute("SELECT id FROM beliefs WHERE topic = 'rev-test' LIMIT 1").fetchone()
    conn.close()
    assert bid_row is not None
    bid = bid_row[0]

    runner.invoke(
        cli,
        ["add-revision", "--belief", bid, "--new-statement", "New statement.",
         "--new-confidence", "0.7", "--rationale", "evidence"],
        env=env,
    )
    result = runner.invoke(cli, ["show-revisions", "--belief", bid], env=env)
    assert result.exit_code == 0
    assert "evidence" in result.output


def test_inspect_entity(runner_with_db: tuple[CliRunner, dict[str, str]]) -> None:
    runner, env = runner_with_db
    runner.invoke(cli, ["add-entity", "--name", "Inspect-Me", "--type", "lab"], env=env)
    import duckdb
    conn = duckdb.connect(env["MESH_DB_PATH"])
    eid_row = conn.execute(
        "SELECT id FROM entities WHERE canonical_name = 'Inspect-Me'"
    ).fetchone()
    assert eid_row is not None
    eid = eid_row[0]
    conn.close()

    result = runner.invoke(cli, ["inspect", eid], env=env)
    assert result.exit_code == 0
    assert "Inspect-Me" in result.output


def test_inspect_missing_id(runner_with_db: tuple[CliRunner, dict[str, str]]) -> None:
    runner, env = runner_with_db
    result = runner.invoke(cli, ["inspect", "does-not-exist"], env=env)
    assert result.exit_code != 0


def test_show_sources(runner_with_db: tuple[CliRunner, dict[str, str]]) -> None:
    runner, env = runner_with_db
    runner.invoke(
        cli,
        ["add-source", "--type", "blog", "--url", "https://blog.example.com",
         "--published-at", "2024-01-01T00:00:00"],
        env=env,
    )
    result = runner.invoke(cli, ["show-sources"], env=env)
    assert result.exit_code == 0
    assert "blog" in result.output
