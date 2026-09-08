"""The MCP spec renderer: exactly what each campus subprocess is launched with.

Under Hermes this rendered a ``mcp_servers`` block into a container's
config.yaml. Now it renders the arguments Agno hands to
``mcp.StdioServerParameters``, so what is asserted here is the literal command,
argv, environment, and working directory of a real subprocess — including which
student's credentials reach it.
"""

from uuid import UUID

from app.campus.catalog import CAMPUS_TOOLS, normalize_tool_ids
from app.campus.credentials import CampusSecrets
from app.campus.mcp_config import build_server_specs, working_directories

MCP_ROOT = "/opt/mcp"
STATE_ROOT = "/var/lib/devrimo/campus"
USER = UUID("11111111-1111-1111-1111-111111111111")
OTHER_USER = UUID("22222222-2222-2222-2222-222222222222")


def secrets(**overrides) -> CampusSecrets:
    base = {
        "metu_username": "e123456",
        "metu_password": "hunter2",
        "odtuclass_token": "",
        "locale": "tr",
        "odtuclass_base_url": "",
    }
    return CampusSecrets(**{**base, **overrides})


def specs(tool_ids, sec, user=USER):
    return build_server_specs(user, tool_ids, sec, mcp_root=MCP_ROOT, state_root=STATE_ROOT)


def by_id(items):
    return {spec.tool_id: spec for spec in items}


def test_only_enabled_tools_are_rendered():
    assert [s.tool_id for s in specs(["sais"], secrets())] == ["sais"]


def test_spec_is_a_complete_launch_description():
    spec = specs(["sais"], secrets())[0]
    assert spec.command == "/opt/mcp/sais/.venv/bin/python"
    assert spec.args == ("-m", "sais_mcp.server", "--transport", "stdio")
    assert spec.env == {"SAIS_USERNAME": "e123456", "SAIS_PASSWORD": "hunter2", "LOCALE": "tr"}
    assert spec.cwd == f"/var/lib/devrimo/campus/{USER}/sais"


def test_odtuclass_script_path_resolves_against_the_install_root():
    # It ships loose modules rather than an installable package, so it is
    # launched by path — which is only knowable once the root is.
    spec = by_id(specs(["odtuclass"], secrets()))["odtuclass"]
    assert spec.args == ("/opt/mcp/odtuclass/odtuclass_mcp.py",)


def test_tools_without_their_credentials_are_dropped():
    # No password and no ODTUClass token: nothing can authenticate, so no
    # server is launched rather than four that fail at runtime.
    assert specs([t.id for t in CAMPUS_TOOLS], secrets(metu_password="")) == []


def test_odtuclass_works_from_a_token_alone():
    rendered = specs(["odtuclass", "sais"], secrets(metu_password="", odtuclass_token="tok123"))
    assert [s.tool_id for s in rendered] == ["odtuclass"]
    assert rendered[0].env["ODTUCLASS_TOKEN"] == "tok123"


def test_empty_env_values_are_omitted_not_blanked():
    # A blank ODTUCLASS_TOKEN would make the upstream client prefer an empty
    # token over falling back to username/password login.
    spec = by_id(specs(["odtuclass"], secrets()))["odtuclass"]
    assert "ODTUCLASS_TOKEN" not in spec.env
    assert spec.env["ODTUCLASS_PASSWORD"] == "hunter2"


def test_every_server_runs_from_its_own_virtualenv():
    rendered = specs([t.id for t in CAMPUS_TOOLS], secrets(odtuclass_token="tok"))
    commands = {s.command for s in rendered}
    assert len(commands) == len(rendered)
    assert all(c.startswith("/opt/mcp/") for c in commands)


def test_each_server_gets_only_its_own_declared_credentials():
    # The isolation that used to come from one container per student now comes
    # from process environment, so no server may see a variable it did not ask
    # for — webmail's METU_PASSWORD must not leak into the sais process.
    rendered = by_id(specs([t.id for t in CAMPUS_TOOLS], secrets(odtuclass_token="tok")))
    assert set(rendered["sais"].env) == {"SAIS_USERNAME", "SAIS_PASSWORD", "LOCALE"}
    assert "SAIS_PASSWORD" not in rendered["webmail"].env
    assert "METU_PASSWORD" not in rendered["sais"].env


def test_two_students_never_share_a_working_directory():
    mine = working_directories(specs([t.id for t in CAMPUS_TOOLS], secrets()))
    theirs = working_directories(specs([t.id for t in CAMPUS_TOOLS], secrets(), user=OTHER_USER))
    assert mine and theirs
    assert set(mine).isdisjoint(theirs)
    assert all(str(USER) in d for d in mine)


def test_no_secrets_without_a_bundle():
    assert specs(["sais"], None) == []


def test_unknown_tool_ids_are_ignored():
    assert normalize_tool_ids(["sais", "not-a-tool"]) == ["sais"]


def test_normalize_preserves_catalog_order_and_dedupes():
    assert normalize_tool_ids(["webmail", "sais", "sais"]) == ["sais", "webmail"]


def test_secrets_repr_does_not_leak_the_password():
    assert "hunter2" not in repr(secrets())


def test_spec_repr_does_not_leak_the_password():
    # Specs are held on the pool entry and land in log lines far from here.
    spec = specs(["sais"], secrets())[0]
    assert "hunter2" not in repr(spec)
    assert "hunter2" not in repr(spec.describe())
    assert spec.describe()["env_keys"] == ["LOCALE", "SAIS_PASSWORD", "SAIS_USERNAME"]
