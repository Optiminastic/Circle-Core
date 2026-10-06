from tests.fakes import InMemoryDocumentRepository


def test_in_memory_repository_round_trip() -> None:
    repo = InMemoryDocumentRepository()
    repo.upsert("t", "a", {"id": "a", "candidateId": "c1"})
    repo.upsert("t", "b", {"id": "b", "candidateId": "c2"})

    assert repo.get("t", "a") == {"id": "a", "candidateId": "c1"}
    assert [d["id"] for d in repo.find("t", {"candidateId": "c2"})] == ["b"]
    assert repo.count("t") == 2
    assert repo.delete("t", "a") is True
    assert repo.get("t", "a") is None
