from core.app import run_application


def test_application(capsys):
    run_application()
    assert "py_upper" in capsys.readouterr().out
