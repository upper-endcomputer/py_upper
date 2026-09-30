from myapp.core.app import Application


def test_application():
    app = Application()
    assert app.run() == 0
    assert app.state.started is True
