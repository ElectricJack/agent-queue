@pytest.fixture
def json_args():
    descriptor = _snapshot(handlers=[HandlerCommand("generic", "Generic", True)],
                           fallback={"generic": SCHEMA}).commands["generic"]
    return argument_model(descriptor)


