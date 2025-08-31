from projectwise import create_app
from projectwise.routes.chat import TodoIn


async def test_echo() -> None:
    app = await create_app()
    test_client = app.test_client()
    response = await test_client.post("/chat/echo", json={"a": "b"})
    data = await response.get_json()
    assert data == {"extra": True, "input": {"a": "b"}}


async def test_todo() -> None:
    app = await create_app()
    test_client = app.test_client()
    response = await test_client.post("/chat/todo/", json=TodoIn(task="test", due=None))
    data = await response.get_json()
    assert data == {"id": 1, "task": "test", "due": None}
