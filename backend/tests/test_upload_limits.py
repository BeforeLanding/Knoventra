import io
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from knoventra.api.deps import get_db
from knoventra.api import routes
from knoventra.config import Settings, settings
from knoventra.ingestion.uploads import UploadTooLarge, copy_upload
from knoventra.main import app


class BoundedReader(io.BytesIO):
    def read(self, size=-1):
        assert 0 < size <= 64 * 1024, "unbounded reads are forbidden"
        return super().read(size)


@pytest.mark.parametrize("size", [1, 65536, 65537])
def test_copy_accepts_exact_limit_with_bounded_reads(tmp_path, size):
    target = tmp_path / "file.md"
    data = b"x" * size
    assert copy_upload(BoundedReader(data), target, size) == size
    assert target.read_bytes() == data


def test_copy_stops_after_one_excess_byte_without_writing_it(tmp_path):
    source = BoundedReader(b"x" * 200000)
    target = tmp_path / "file.md"
    with pytest.raises(UploadTooLarge):
        copy_upload(source, target, 65536)
    assert source.tell() == 65537
    assert target.stat().st_size == 65536


def test_empty_file_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="empty"):
        copy_upload(BoundedReader(b""), tmp_path / "empty.md", 100)


@pytest.mark.parametrize("limit", [0, -1])
def test_nonpositive_limit_is_rejected_by_configuration(limit):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, upload_max_bytes=limit)


@pytest.mark.parametrize("body,status", [(b"", 422), (b"x" * 33, 413)])
def test_rejected_upload_never_loads_encoder_or_creates_revision(db, monkeypatch, body, status):
    monkeypatch.setattr(settings, "upload_max_bytes", 32)
    encoder = Mock(side_effect=AssertionError("must not load encoder"))
    monkeypatch.setattr(routes, "get_encoder", encoder)
    observed_paths = []
    original = routes.copy_upload
    def observed_copy(source, destination, limit):
        observed_paths.append(Path(destination))
        return original(source, destination, limit)
    monkeypatch.setattr(routes, "copy_upload", observed_copy)
    app.dependency_overrides[get_db] = lambda: db
    try:
        with TestClient(app) as client:
            token = client.post('/api/v1/auth/login', json={'email': 'admin@example.test', 'password': 'demo-pass-1'}).json()['token']
            headers = {'Authorization': 'Bearer ' + token}
            before = client.get('/api/v1/admin/documents', headers=headers).json()
            assert before['upload_max_bytes'] == 32
            response = client.post('/api/v1/admin/documents', headers=headers,
                                   data={'kb': 'it-public', 'title': 'VPN 使用说明', 'lang': 'zh-Hans'},
                                   files={'file': ('bounded.md', body, 'text/markdown')})
            assert response.status_code == status
            if status == 413:
                assert response.json()['detail'] == {'code': 'UPLOAD_TOO_LARGE', 'max_bytes': 32}
            assert client.get('/api/v1/admin/documents', headers=headers).json() == before
    finally:
        app.dependency_overrides.clear()
    encoder.assert_not_called()
    assert observed_paths and all(not path.exists() for path in observed_paths)
