from services.hello import make_message


def test_message():
    assert "py_upper" in make_message()
