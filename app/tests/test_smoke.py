from myapp.services.hello import make_message

def test_message():
    assert "PyStand2" in make_message()
