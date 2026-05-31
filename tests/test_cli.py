from __future__ import annotations

import pytest
from click.testing import CliRunner
from mesh_cli.main import cli
from mesh_db.connection import get_connection


@pytest.fixture
def runner_with_db() -> tuple[CliRunner, dict[str, str]]:
    # The store is the session Postgres container (MESH_PG_URL, set by the
    # conftest fixture and inherited by the in-process CLI). MESH_DB_PATH is
    # vestigial post-migration; kept only so the env dict is non-empty.
    env = {"MESH_DB_PATH": "unused-postgres-backed"}
    runner = CliRunner()
    result = runner.invoke(cli, ["init-db"], env=env)
    assert result.exit_code == 0, result.output
    return runner, env


def _first_id(table: str, where: str = "") -> str | None:
    conn = get_connection(read_only=True)
    try:
        row = conn.execute(f"SELECT id FROM {table}{where} LIMIT 1").fetchone()
        return str(row[0]) if row else None
    finally:
        conn.close()


def test_init_db(runner_with_db: tuple[CliRunner, dict[str, str]]) -> None:
    runner, env = runner_with_db
    # Schema is up: a read command succeeds against the initialized store.
    result = runner.invoke(cli, ["show-entities"], env=env)
    assert result.exit_code == 0, result.output


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
    r1 = runner.invoke(cli, ["add-entity", "--name", "GPT-4", "--type", "model"], env=env)
    assert r1.exit_code == 0
    r2 = runner.invoke(
        cli,
        ["add-source", "--type", "arxiv", "--url", "https://arxiv.org/abs/test",
         "--published-at", "2023-01-01T00:00:00"],
        env=env,
    )
    assert r2.exit_code == 0

    eid = _first_id("entities")
    sid = _first_id("sources")
    assert eid is not None and sid is not None

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
    bid = _first_id("beliefs")
    assert bid is not None

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
    bid = _first_id("beliefs", where=" WHERE topic = 'rev-test'")
    assert bid is not None

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
    eid = _first_id("entities", where=" WHERE canonical_name = 'Inspect-Me'")
    assert eid is not None

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


def test_heuristics_list_empty(runner_with_db: tuple[CliRunner, dict[str, str]]) -> None:
    runner, env = runner_with_db
    result = runner.invoke(cli, ["heuristics", "list"], env=env)
    assert result.exit_code == 0
    assert "No heuristics recorded" in result.output


def test_heuristics_list_after_persist(
    runner_with_db: tuple[CliRunner, dict[str, str]],
) -> None:
    runner, env = runner_with_db
    from mesh_agents.consolidator import HeuristicProposal
    from mesh_pipeline._heuristics import persist_heuristic

    conn = get_connection()
    try:
        persist_heuristic(
            conn,
            HeuristicProposal(
                agent="claim_extractor",
                skill="extract_claims",
                source="reddit",
                heuristic="Forum scores are self-reported; lower confidence.",
                provenance_run_ids=["run-1"],
                rationale="seen repeatedly",
            ),
        )
    finally:
        conn.close()

    # Widen the rich console so cell contents aren't truncated for the asserts.
    wide = {**env, "COLUMNS": "200"}
    result = runner.invoke(cli, ["heuristics", "list"], env=wide)
    assert result.exit_code == 0, result.output
    assert "claim_extractor" in result.output
    assert "extract_claims" in result.output
    assert "reddit" in result.output

    # Skill filter narrows the set.
    miss = runner.invoke(
        cli, ["heuristics", "list", "--skill", "challenge_belief"], env=wide
    )
    assert miss.exit_code == 0
    assert "No heuristics recorded" in miss.output
