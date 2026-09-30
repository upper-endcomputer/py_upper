from importlib.resources import files


def resource_root():
    return files("myapp").joinpath("resources")
