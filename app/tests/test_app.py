from myapp.core.app import run_application


def test_application(capsys):
    run_application()
    assert "PyStand2" in capsys.readouterr().out
