"""Storage encryption with a customer-provided key (issue #421).

The provider SDKs are mocked: what is checked is that every request that
carries content carries the key, that plain objects stay readable, that the
one-way lock holds, and that the job encrypts what was stored before.
cloudpathlib's offline clients stand in for the buckets the lock and the job
work on.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

from pincer.config.main import _RelaxedSettings
from pincer.storage import clients, encrypt_job, encryption_state
from pincer.storage.browser import StorageBrowser
from pincer.storage.encryption import (
    TLS_WARNING_DOC,
    InvalidStorageKey,
    StorageKey,
    keyed_request_hit_plain_object,
    load_key,
    parse_key,
    tls_problem,
)
from pincer.storage.errors import KeyConflict, StorageEncryptionError, StorageUnavailable

if TYPE_CHECKING:
    from collections.abc import Callable

pytest.importorskip("cloudpathlib")

KEY_B64 = base64.b64encode(bytes(range(32))).decode()
OTHER_B64 = base64.b64encode(bytes(range(1, 33))).decode()
KEY = parse_key(KEY_B64)


def _settings(tmp_path: Path, **overrides: Any) -> Any:
    values: dict[str, Any] = {"data_dir": tmp_path / "data", **overrides}
    return _RelaxedSettings(**values)  # type: ignore[call-arg]


def _client_error(status: int, code: str, op: str = "HeadObject") -> Exception:
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}, op)


# ── The key ──────────────────────────────────────────────────────────────────


def test_a_key_is_32_bytes_of_base64_and_never_shown() -> None:
    assert KEY.raw == bytes(range(32))
    assert KEY_B64 not in repr(KEY)
    assert KEY.fingerprint == parse_key(KEY_B64).fingerprint
    assert KEY.fingerprint != parse_key(OTHER_B64).fingerprint
    assert KEY_B64 not in KEY.fingerprint


@pytest.mark.parametrize("value", ["not base64!", base64.b64encode(b"short").decode()])
def test_a_bad_key_is_refused_without_echoing_it(value: str) -> None:
    with pytest.raises(InvalidStorageKey) as caught:
        parse_key(value)
    assert value not in str(caught.value)


def test_settings_refuse_a_bad_key_and_treat_empty_as_off(tmp_path: Path) -> None:
    with pytest.raises(InvalidStorageKey, match="32 bytes"):
        _settings(tmp_path, storage_encryption_key=base64.b64encode(b"x" * 16).decode())
    assert load_key(_settings(tmp_path, storage_encryption_key="")) is None
    assert load_key(_settings(tmp_path, storage_encryption_key=KEY_B64)) == KEY


def test_a_bad_key_in_settings_never_reaches_the_error_message(tmp_path: Path) -> None:
    """pydantic prints the raw input of a ValueError raised in a validator; the key must not be there."""
    import traceback

    bad = base64.b64encode(b"secret-key-too-short").decode()
    with pytest.raises(InvalidStorageKey) as caught:
        _settings(tmp_path, storage_encryption_key=bad)
    shown = "".join(traceback.format_exception(caught.value))
    assert bad not in shown
    assert "secret-key" not in shown


def test_provider_forms_of_the_key() -> None:
    args = KEY.s3_args()
    assert args["SSECustomerAlgorithm"] == "AES256"
    assert base64.b64decode(args["SSECustomerKey"]) == KEY.raw
    assert set(KEY.s3_copy_source_args()) == {
        "CopySourceSSECustomerAlgorithm",
        "CopySourceSSECustomerKey",
        "CopySourceSSECustomerKeyMD5",
    }
    cpk = KEY.azure_cpk()
    assert cpk.key_value == KEY.b64
    assert cpk.key_hash == KEY.sha256_b64


# ── TLS ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("scheme", "overrides"),
    [
        ("s3", {"storage_s3_endpoint_url": "http://minio:9000"}),
        ("az", {"storage_azure_account_url": "http://127.0.0.1:10000/devstoreaccount1"}),
        (
            "az",
            {"storage_azure_connection_string": "DefaultEndpointsProtocol=http;AccountName=a;AccountKey=dGVzdA=="},
        ),
        ("az", {"storage_azure_connection_string": "BlobEndpoint=http://azurite:10000/a;AccountName=a"}),
        ("az", {"storage_azure_connection_string": "UseDevelopmentStorage=true"}),
    ],
)
def test_a_non_tls_endpoint_cannot_carry_the_key(tmp_path: Path, scheme: str, overrides: dict[str, Any]) -> None:
    settings = _settings(tmp_path, storage_encryption_key=KEY_B64, **overrides)
    assert tls_problem(scheme, settings)
    with pytest.raises(StorageEncryptionError) as caught:
        clients.build_client(scheme, settings, None)
    assert "only be read over TLS" in str(caught.value)
    assert TLS_WARNING_DOC in str(caught.value)


def test_a_tls_endpoint_is_fine(tmp_path: Path) -> None:
    settings = _settings(tmp_path, storage_s3_endpoint_url="https://minio:9000")
    assert tls_problem("s3", settings) is None
    assert tls_problem("gs", settings) is None


def test_a_non_tls_endpoint_without_a_key_is_left_alone(tmp_path: Path) -> None:
    settings = _settings(tmp_path, storage_s3_endpoint_url="http://minio:9000", storage_s3_access_key_id="a")
    assert type(clients.build_client("s3", settings, None)).__name__ == "S3Client"


# ── Which errors mean "this object is stored without the key" ───────────────


def test_encrypted_object_errors() -> None:
    from azure.core.exceptions import HttpResponseError
    from google.api_core.exceptions import BadRequest

    from pincer.storage.encryption import request_hit_encrypted_object

    assert request_hit_encrypted_object(_client_error(400, "InvalidRequest", "GetObject"))
    assert not request_hit_encrypted_object(_client_error(404, "NoSuchKey", "GetObject"))
    assert request_hit_encrypted_object(BadRequest("CSEK object"))
    azure = HttpResponseError("encrypted")
    azure.error_code = "BlobUsesCustomerSpecifiedEncryption"
    assert request_hit_encrypted_object(azure)
    azure.error_code = "ServerBusy"
    assert not request_hit_encrypted_object(azure)


def test_plain_object_errors() -> None:
    assert keyed_request_hit_plain_object(_client_error(400, "400"))
    assert keyed_request_hit_plain_object(_client_error(400, "InvalidRequest", "GetObject"))
    assert not keyed_request_hit_plain_object(_client_error(403, "AccessDenied"))
    assert not keyed_request_hit_plain_object(_client_error(404, "404"))

    from azure.core.exceptions import HttpResponseError
    from google.api_core.exceptions import BadRequest, Forbidden

    assert keyed_request_hit_plain_object(BadRequest("not a CSEK object"))
    assert not keyed_request_hit_plain_object(Forbidden("wrong key"))
    azure = HttpResponseError("plain")
    azure.error_code = "BlobDoesNotUseCustomerSpecifiedEncryption"
    assert keyed_request_hit_plain_object(azure)
    azure.error_code = "AuthorizationFailure"
    assert not keyed_request_hit_plain_object(azure)


# ── S3 (SSE-C) ───────────────────────────────────────────────────────────────


def _s3(tmp_path: Path) -> Any:
    settings = _settings(
        tmp_path,
        storage_encryption_key=KEY_B64,
        storage_s3_endpoint_url="https://s3.example.test",
        storage_s3_region="eu-central-1",
        storage_s3_access_key_id="AKIATEST",
        storage_s3_secret_access_key="secret",
    )
    client = clients.build_client("s3", settings, tmp_path / "cache")
    client.client = MagicMock()
    client.client.exceptions.NoSuchKey = type("NoSuchKey", (Exception,), {})
    client.s3 = MagicMock()
    return client


def _head(**extra: Any) -> dict[str, Any]:
    from datetime import UTC, datetime

    return {"LastModified": datetime.now(UTC), "ContentLength": 3, "ETag": '"e"', "Metadata": {}, **extra}


def test_s3_client_is_the_encrypting_one_and_keys_uploads(tmp_path: Path) -> None:
    from pincer.storage.encrypted_clients import EncryptedS3Client

    client = _s3(tmp_path)
    assert isinstance(client, EncryptedS3Client)
    assert client.boto3_ul_extra_args["SSECustomerKey"] == KEY.b64
    assert client.boto3_copy_extra_args["SSECustomerAlgorithm"] == "AES256"


def test_s3_metadata_is_read_with_the_key(tmp_path: Path) -> None:
    client = _s3(tmp_path)
    client.client.head_object.return_value = _head()
    meta = client._get_metadata(client.CloudPath("s3://b/k"))
    assert meta["size"] == 3
    assert client.client.head_object.call_args.kwargs["SSECustomerKey"] == KEY.b64


def test_s3_a_plain_object_is_read_without_the_key(tmp_path: Path) -> None:
    client = _s3(tmp_path)
    client.client.head_object.side_effect = [_client_error(400, "400"), _head()]
    path = client.CloudPath("s3://b/old.txt")
    assert client.is_encrypted(path) is False
    assert "SSECustomerKey" not in client.client.head_object.call_args.kwargs


def test_s3_an_object_under_another_key_is_an_encryption_error(tmp_path: Path) -> None:
    client = _s3(tmp_path)
    client.client.head_object.side_effect = [_client_error(400, "400"), _head(SSECustomerAlgorithm="AES256")]
    with pytest.raises(StorageEncryptionError, match="different customer key"):
        client._get_metadata(client.CloudPath("s3://b/k"))


def test_s3_an_object_under_another_key_is_a_key_error_not_missing(tmp_path: Path) -> None:
    """A wrong SSE-C key is 403 on the keyed HEAD; a plain HEAD of the SSE-C object is 400."""
    client = _s3(tmp_path)
    client.client.head_object.side_effect = [_client_error(403, "AccessDenied"), _client_error(400, "400")]
    with pytest.raises(StorageEncryptionError, match="different customer key"):
        client._s3_file_query(client.CloudPath("s3://b/theirs"))


def test_s3_a_missing_permission_stays_a_permission_error(tmp_path: Path) -> None:
    from botocore.exceptions import ClientError

    client = _s3(tmp_path)
    client.client.head_object.side_effect = [_client_error(403, "AccessDenied"), _client_error(403, "AccessDenied")]
    with pytest.raises(ClientError):
        client._get_metadata(client.CloudPath("s3://b/k"))


def test_s3_a_plain_file_is_still_a_file(tmp_path: Path) -> None:
    """cloudpathlib's own HEAD with the key would take a plain object for a missing one."""
    client = _s3(tmp_path)
    client.client.head_object.side_effect = [_client_error(400, "400"), _head()]
    assert client._s3_file_query(client.CloudPath("s3://b/old.txt")) == "file"


def test_s3_download_falls_back_for_a_plain_object(tmp_path: Path) -> None:
    client = _s3(tmp_path)
    obj = client.s3.Object.return_value
    obj.download_file.side_effect = [_client_error(400, "400"), None]
    client._download_file(client.CloudPath("s3://b/old.txt"), tmp_path / "out")
    first, second = obj.download_file.call_args_list
    assert first.kwargs["ExtraArgs"]["SSECustomerKey"] == KEY.b64
    assert "SSECustomerKey" not in second.kwargs["ExtraArgs"]


def test_s3_copy_reads_an_encrypted_source_with_the_key(tmp_path: Path) -> None:
    client = _s3(tmp_path)
    client.client.head_object.return_value = _head()
    client._move_file(client.CloudPath("s3://b/a"), client.CloudPath("s3://b/c"), remove_src=False)
    extra = client.s3.Object.return_value.copy.call_args.kwargs["ExtraArgs"]
    assert extra["SSECustomerKey"] == KEY.b64
    assert extra["CopySourceSSECustomerKey"] == KEY.b64


def test_s3_copy_of_a_plain_source_writes_it_encrypted(tmp_path: Path) -> None:
    client = _s3(tmp_path)
    client.client.head_object.side_effect = [_client_error(400, "400"), _head()]
    client._move_file(client.CloudPath("s3://b/a"), client.CloudPath("s3://b/c"), remove_src=False)
    extra = client.s3.Object.return_value.copy.call_args.kwargs["ExtraArgs"]
    assert extra["SSECustomerKey"] == KEY.b64
    assert "CopySourceSSECustomerKey" not in extra


def test_s3_encrypt_in_place_keeps_headers_and_is_conditional(tmp_path: Path) -> None:
    client = _s3(tmp_path)
    client.client.head_object.return_value = _head(ContentType="text/csv", Metadata={"owner": "x"})
    client.encrypt_in_place(client.CloudPath("s3://b/data.csv"))
    extra = client.s3.Object.return_value.copy.call_args.kwargs["ExtraArgs"]
    assert extra["CopySourceIfMatch"] == '"e"'
    assert extra["ContentType"] == "text/csv"
    assert extra["Metadata"] == {"owner": "x"}
    assert extra["MetadataDirective"] == "REPLACE"
    assert extra["SSECustomerKey"] == KEY.b64
    assert "CopySourceSSECustomerKey" not in extra


def test_s3_encrypt_in_place_of_a_changed_object_is_a_conflict(tmp_path: Path) -> None:
    client = _s3(tmp_path)
    client.client.head_object.return_value = _head()
    client.s3.Object.return_value.copy.side_effect = _client_error(412, "PreconditionFailed", "CopyObject")
    with pytest.raises(KeyConflict):
        client.encrypt_in_place(client.CloudPath("s3://b/k"))


def test_s3_objects_send_the_key_and_have_no_provider_links(tmp_path: Path) -> None:
    import io

    from pincer.storage.objects import objects_for

    client = _s3(tmp_path)
    objects = objects_for(client.CloudPath("s3://b/"))
    assert objects.presignable is False
    objects.write(client.CloudPath("s3://b/k"), io.BytesIO(b"x"), content_type="text/plain")
    assert client.client.put_object.call_args.kwargs["SSECustomerKey"] == KEY.b64

    body = MagicMock()
    body.read.return_value = b"old"
    plain_error = _client_error(400, "InvalidRequest", "GetObject")
    client.client.get_object.side_effect = [plain_error, {"Body": body, "ETag": "e"}]
    assert objects.read(client.CloudPath("s3://b/old"))[0] == b"old"


# ── Google Cloud Storage (CSEK) ──────────────────────────────────────────────


def _gs() -> Any:
    from pincer.storage.encrypted_clients import EncryptedGSClient

    return EncryptedGSClient(storage_client=MagicMock(), encryption_key=KEY)


def _gcs_blob(keyed: bool) -> MagicMock:
    blob = MagicMock()
    blob._properties = {"customerEncryption": {"keySha256": "x"}} if keyed else {}
    blob.generation = 7
    return blob


def test_gs_download_keys_only_an_encrypted_blob(tmp_path: Path) -> None:
    client = _gs()
    for keyed in (True, False):
        blob = _gcs_blob(keyed)
        client.client.bucket.return_value.get_blob.return_value = blob
        client._download_file(client.CloudPath("gs://b/k"), tmp_path / "out")
        if keyed:
            assert blob.encryption_key == KEY.raw
        else:
            assert not isinstance(blob.encryption_key, bytes)
        blob.download_to_filename.assert_called_once()


def test_gs_upload_is_keyed(tmp_path: Path) -> None:
    client = _gs()
    (tmp_path / "f.txt").write_text("x")
    client._upload_file(tmp_path / "f.txt", client.CloudPath("gs://b/f.txt"))
    client.client.bucket.return_value.blob.assert_called_with("f.txt", encryption_key=KEY.raw)


def test_gs_copy_is_a_keyed_rewrite_until_done() -> None:
    client = _gs()
    bucket = client.client.bucket.return_value
    bucket.get_blob.return_value = _gcs_blob(keyed=True)
    target = bucket.blob.return_value
    target.rewrite.side_effect = [("token", 1, 2), (None, 2, 2)]
    client._move_file(client.CloudPath("gs://b/a"), client.CloudPath("gs://b/c"), remove_src=True)
    assert target.rewrite.call_count == 2
    assert target.rewrite.call_args.kwargs["token"] == "token"
    bucket.get_blob.return_value.delete.assert_called_once()


def test_gs_encrypt_in_place_is_conditional_on_the_generation() -> None:
    from google.api_core.exceptions import PreconditionFailed

    client = _gs()
    bucket = client.client.bucket.return_value
    bucket.get_blob.return_value = _gcs_blob(keyed=False)
    bucket.blob.return_value.rewrite.return_value = (None, 1, 1)
    client.encrypt_in_place(client.CloudPath("gs://b/k"))
    assert bucket.blob.return_value.rewrite.call_args.kwargs["if_source_generation_match"] == 7
    assert client.is_encrypted(client.CloudPath("gs://b/k")) is False

    bucket.blob.return_value.rewrite.side_effect = PreconditionFailed("changed")
    with pytest.raises(KeyConflict):
        client.encrypt_in_place(client.CloudPath("gs://b/k"))


def test_gs_objects_read_with_the_key_only_when_stored_with_one() -> None:
    from pincer.storage.objects import GSObjects

    client = _gs()
    objects = GSObjects(client, KEY)
    blob = _gcs_blob(keyed=True)
    blob.download_as_bytes.return_value = b"secret"
    client.client.bucket.return_value.get_blob.return_value = blob
    assert objects.read(client.CloudPath("gs://b/k"))[0] == b"secret"
    assert blob.encryption_key == KEY.raw
    assert objects.presignable is False


# ── Azure Blob (CPK) and Data Lake (HNS) ─────────────────────────────────────


def _azure(hns: bool = False) -> Any:
    from pincer.storage.encrypted_clients import EncryptedAzureBlobClient

    client = EncryptedAzureBlobClient(blob_service_client=MagicMock(), data_lake_client=MagicMock(), encryption_key=KEY)
    client._hns_enabled = hns
    return client


def _azure_error(code: str, cls: Any = None) -> Exception:
    from azure.core.exceptions import HttpResponseError

    error = (cls or HttpResponseError)(f"{code} happened")
    error.error_code = code
    return error


def test_azure_properties_download_and_upload_carry_the_key(tmp_path: Path) -> None:
    client = _azure()
    blob = client.service_client.get_blob_client.return_value
    blob.get_blob_properties.return_value = MagicMock(encryption_key_sha256=KEY.sha256_b64)
    path = client.CloudPath("az://c/k")
    client._get_metadata(path)
    assert blob.get_blob_properties.call_args.kwargs["cpk"].key_hash == KEY.sha256_b64
    assert client.is_encrypted(path) is True

    (tmp_path / "f").write_text("x")
    client._upload_file(tmp_path / "f", path)
    assert blob.upload_blob.call_args.kwargs["cpk"].key_value == KEY.b64

    client._download_file(path, tmp_path / "out")
    assert "cpk" in blob.download_blob.call_args.kwargs


def test_azure_a_plain_blob_is_read_without_the_key() -> None:
    client = _azure()
    blob = client.service_client.get_blob_client.return_value
    plain = MagicMock(encryption_key_sha256=None)
    blob.get_blob_properties.side_effect = [_azure_error("BlobDoesNotUseCustomerSpecifiedEncryption"), plain]
    assert client.is_encrypted(client.CloudPath("az://c/old")) is False
    assert blob.get_blob_properties.call_args.kwargs == {}


def test_azure_copy_streams_and_writes_encrypted() -> None:
    client = _azure()
    blob = client.service_client.get_blob_client.return_value
    downloader = blob.download_blob.return_value
    client._move_file(client.CloudPath("az://c/a"), client.CloudPath("az://c/b"), remove_src=False)
    kwargs = blob.upload_blob.call_args.kwargs
    assert kwargs["cpk"].key_value == KEY.b64
    assert kwargs["metadata"] is downloader.properties.metadata
    blob.start_copy_from_url.assert_not_called()


def test_azure_encrypt_in_place_is_conditional_on_the_etag() -> None:
    from azure.core.exceptions import ResourceModifiedError

    client = _azure()
    blob = client.service_client.get_blob_client.return_value
    blob.download_blob.return_value.properties.etag = "0x1"
    client.encrypt_in_place(client.CloudPath("az://c/k"))
    kwargs = blob.upload_blob.call_args.kwargs
    assert kwargs["etag"] == "0x1"
    assert kwargs["cpk"].key_value == KEY.b64

    blob.upload_blob.side_effect = ResourceModifiedError("changed")
    with pytest.raises(KeyConflict):
        client.encrypt_in_place(client.CloudPath("az://c/k"))


def test_hns_file_properties_go_through_data_lake_with_the_key() -> None:
    client = _azure(hns=True)
    file_client = client.data_lake_client.get_file_system_client.return_value.get_file_client.return_value
    file_client.get_file_properties.return_value = {"content_settings": {"content_type": "text/plain"}}
    meta = client._get_metadata(client.CloudPath("az://c/dir/f.txt"))
    assert meta["content_type"] == "text/plain"
    assert file_client.get_file_properties.call_args.kwargs["cpk"].key_hash == KEY.sha256_b64


def test_hns_rename_needs_no_key_and_maps_errors() -> None:
    client = _azure(hns=True)
    fsc = client.data_lake_client.get_file_system_client.return_value
    fsc.get_file_client.return_value.get_file_properties.return_value = {"is_directory": False}
    src, dst = client.CloudPath("az://c/a.txt"), client.CloudPath("az://c/b.txt")
    with (
        patch.object(type(client).__mro__[1], "_move_file", side_effect=_azure_error("AuthorizationFailure")),
        pytest.raises(StorageUnavailable, match="rename of az://c/a.txt failed \\(AuthorizationFailure\\)"),
    ):
        client._move_file(src, dst)


@pytest.mark.parametrize(
    ("raised", "expected", "message"),
    [
        ("BlobUsesCustomerSpecifiedEncryption", StorageEncryptionError, "refused for the customer-provided key"),
        ("CustomerProvidedKeyMismatch", StorageEncryptionError, "refused for the customer-provided key"),
        ("ServerBusy", StorageUnavailable, "failed \\(ServerBusy\\)"),
    ],
)
def test_datalake_errors_maps_service_errors(raised: str, expected: type[Exception], message: str) -> None:
    from pincer.storage.encrypted_clients import datalake_errors

    with pytest.raises(expected, match=message) as caught, datalake_errors("list", "az://c/x"):
        raise _azure_error(raised)
    assert KEY.b64 not in str(caught.value)
    if expected is StorageUnavailable:
        assert not isinstance(caught.value, StorageEncryptionError)


def test_datalake_errors_lets_not_found_through_and_flags_plain_http() -> None:
    from azure.core.exceptions import ResourceNotFoundError

    from pincer.storage.encrypted_clients import datalake_errors

    with pytest.raises(ResourceNotFoundError), datalake_errors("get file properties", "az://c/x"):
        raise ResourceNotFoundError("gone")
    with pytest.raises(StorageEncryptionError, match="needs TLS"), datalake_errors("rename", "az://c/x"):
        raise ValueError("Customer provided encryption key must be used over HTTPS.")


def test_hns_listing_and_delete_and_mkdir_are_wrapped() -> None:
    client = _azure(hns=True)
    fsc = client.data_lake_client.get_file_system_client.return_value
    fsc.get_paths.side_effect = _azure_error("ServerBusy")
    with pytest.raises(StorageUnavailable, match="list"):
        list(client._list_dir(client.CloudPath("az://c/dir")))
    fsc.get_directory_client.return_value.exists.side_effect = _azure_error("BlobUsesCustomerSpecifiedEncryption")
    with pytest.raises(StorageEncryptionError, match="create directory"):
        client._mkdir(client.CloudPath("az://c/dir"), parents=True, exist_ok=False)


def test_hns_probe_passes_every_step() -> None:
    client = _azure(hns=True)
    blob = client.service_client.get_blob_client.return_value
    uploaded: list[bytes] = []
    blob.upload_blob.side_effect = lambda data, **_: uploaded.append(data)
    blob.download_blob.return_value.readall.side_effect = lambda: uploaded[0]
    fc = client.data_lake_client.get_file_system_client.return_value.get_file_client.return_value
    fc.get_file_properties.return_value = {"encryption_key_sha256": KEY.sha256_b64}

    steps = client.probe_hns(client.CloudPath("az://c/.pincer"))
    assert [problem for _, problem in steps] == [None] * 5
    assert fc.get_file_properties.call_args.kwargs["cpk"].key_hash == KEY.sha256_b64
    fc.rename_file.assert_called_once()
    fc.delete_file.assert_called_once()


def test_hns_probe_stops_at_the_failed_step_and_cleans_up() -> None:
    client = _azure(hns=True)
    blob = client.service_client.get_blob_client.return_value
    uploaded: list[bytes] = []
    blob.upload_blob.side_effect = lambda data, **_: uploaded.append(data)
    blob.download_blob.return_value.readall.side_effect = lambda: uploaded[0]
    fc = client.data_lake_client.get_file_system_client.return_value.get_file_client.return_value
    fc.get_file_properties.return_value = {}
    fc.rename_file.side_effect = _azure_error("BlobUsesCustomerSpecifiedEncryption")

    steps = client.probe_hns(client.CloudPath("az://c/.pincer"))
    assert [name for name, _ in steps][-1] == "rename through Data Lake"
    assert "BlobUsesCustomerSpecifiedEncryption" in (steps[-1][1] or "")
    assert all(problem is None for _, problem in steps[:-1])
    fc.delete_file.assert_not_called()
    assert blob.delete_blob.call_count == 2  # both probe names, whatever state they were left in


# ── The one-way lock ─────────────────────────────────────────────────────────


@pytest.fixture
def bucket(tmp_path: Path, mock_cloud_client: Callable[..., Any]) -> Callable[..., tuple[Any, Any]]:
    """`bucket(key)` → (settings, root) for `s3://docs` on cloudpathlib's offline S3,
    with every client Pincer builds patched to that one."""
    client = mock_cloud_client("s3")

    def make(key: str | None = KEY_B64) -> tuple[Any, Any]:
        settings = _settings(tmp_path, storage_root="s3://docs/", storage_encryption_key=key or "")
        patcher = patch("pincer.storage.clients.build_client", return_value=client)
        patcher.start()
        opened.append(patcher)
        return settings, client.CloudPath("s3://docs/")

    opened: list[Any] = []
    yield make  # type: ignore[misc]
    for patcher in reversed(opened):  # nested patches of one name unwind last-in, first-out
        patcher.stop()


def _open(settings: Any) -> Any:
    return clients.open_cloud_uri("s3://docs/workspace", settings, None, error=StorageUnavailable, what="workspace")


def test_enabling_writes_the_marker(bucket: Callable[..., tuple[Any, Any]]) -> None:
    settings, root = bucket()
    _open(settings)
    marker = json.loads((root / ".pincer" / "encryption.json").read_text())
    assert marker["fingerprint"] == KEY.fingerprint
    assert marker["migration"]["state"] == "pending"
    assert KEY_B64 not in json.dumps(marker)


def test_the_key_cannot_change(bucket: Callable[..., tuple[Any, Any]]) -> None:
    settings, _ = bucket()
    _open(settings)
    encryption_state.reset_for_tests()
    other, _ = bucket(OTHER_B64)
    with pytest.raises(StorageEncryptionError, match="cannot be changed"):
        _open(other)


def test_encryption_cannot_be_turned_off(bucket: Callable[..., tuple[Any, Any]]) -> None:
    settings, _ = bucket()
    _open(settings)
    encryption_state.reset_for_tests()
    off, _ = bucket(None)
    results = encryption_state.verify_at_startup(off)
    assert results[0][1] is not None and "cannot be turned off" in results[0][1]
    # Refused for the rest of the process, with the same reason, under the caller's own error.
    with pytest.raises(StorageUnavailable, match="cannot be turned off"):
        _open(off)


def test_without_a_key_an_unencrypted_bucket_opens_and_gets_no_lock(bucket: Callable[..., tuple[Any, Any]]) -> None:
    settings, root = bucket(None)
    with patch.object(encryption_state, "_objects", side_effect=AssertionError("a request on open")):
        _open(settings)  # opening a path makes no request without a key
    assert not (root / ".encrypted").exists()
    assert not (root / ".pincer").exists()
    assert encryption_state.verify_at_startup(settings) == [("s3://docs", None)]


def _finish(root: Any) -> None:
    """The state the encryption job leaves behind: marker complete, check file written."""
    marker, version = encryption_state.read_marker(root)  # type: ignore[misc]
    marker["migration"]["state"] = "complete"
    encryption_state.write_marker(root, marker, if_version=version)
    encryption_state.write_sentinel(root, KEY)


def test_two_replicas_enabling_at_once_share_one_marker(bucket: Callable[..., tuple[Any, Any]]) -> None:
    settings, root = bucket()
    theirs = encryption_state.new_marker(KEY)
    theirs["enabled_at"] = "earlier"
    real_write = encryption_state.write_marker

    def racing_write(location: Any, marker: dict[str, Any], **kw: Any) -> None:
        if kw.get("if_absent"):
            real_write(location, theirs, if_absent=True)  # the other replica got there first
        real_write(location, marker, **kw)

    with patch.object(encryption_state, "write_marker", side_effect=racing_write):
        _open(settings)
    assert json.loads((root / ".pincer" / "encryption.json").read_text())["enabled_at"] == "earlier"


def test_enabling_writes_no_check_file_until_the_data_is_encrypted(bucket: Callable[..., tuple[Any, Any]]) -> None:
    settings, root = bucket()
    _open(settings)
    assert (root / ".pincer" / "encryption.json").exists()
    assert not (root / ".encrypted").exists()


def test_the_check_file_holds_only_the_hmac(bucket: Callable[..., tuple[Any, Any]]) -> None:
    settings, root = bucket()
    _open(settings)
    _finish(root)
    assert (root / ".encrypted").read_text() == KEY.fingerprint
    assert len(KEY.fingerprint) == 64


@pytest.mark.parametrize("deleted", [".encrypted", ".pincer/encryption.json"])
def test_either_lock_object_alone_keeps_encryption_on(bucket: Callable[..., tuple[Any, Any]], deleted: str) -> None:
    settings, root = bucket()
    _open(settings)
    _finish(root)
    (root / deleted).unlink()
    encryption_state.reset_for_tests()
    off, _ = bucket(None)
    ((_, refused),) = encryption_state.verify_at_startup(off)
    assert refused is not None and "cannot be turned off" in refused
    encryption_state.reset_for_tests()
    other, _ = bucket(OTHER_B64)
    with pytest.raises(StorageUnavailable, match="cannot be changed"):
        _open(other)


@pytest.mark.parametrize("deleted", [".encrypted", ".pincer/encryption.json"])
def test_a_deleted_lock_object_is_written_again(bucket: Callable[..., tuple[Any, Any]], deleted: str) -> None:
    settings, root = bucket()
    _open(settings)
    _finish(root)
    (root / deleted).unlink()
    encryption_state.reset_for_tests()
    _open(settings)
    assert (root / deleted).exists()
    assert (root / ".encrypted").read_text() == KEY.fingerprint
    assert encryption_state.status(root)["migration"]["state"] == "complete"


def test_a_check_file_for_another_key_is_a_wrong_key(bucket: Callable[..., tuple[Any, Any]]) -> None:
    settings, root = bucket()
    (root / ".encrypted").write_text(parse_key(OTHER_B64).fingerprint)
    with pytest.raises(StorageUnavailable, match="cannot be changed"):
        _open(settings)


def test_someone_elses_dot_encrypted_file_is_not_the_lock(bucket: Callable[..., tuple[Any, Any]]) -> None:
    settings, root = bucket(None)
    (root / ".encrypted").write_text("a report about encryption")
    # Without a key: not ours, so not a reason to refuse.
    assert encryption_state.verify_at_startup(settings) == [("s3://docs", None)]
    encryption_state.reset_for_tests()
    keyed, _ = bucket()
    with pytest.raises(StorageUnavailable, match="not Pincer's encryption check file"):
        _open(keyed)


def test_an_encrypted_check_file_unreadable_without_the_key_counts_as_there(
    bucket: Callable[..., tuple[Any, Any]],
) -> None:
    """On a real provider the check file cannot be read without the key at all."""
    settings, root = bucket(None)
    unreadable = MagicMock()
    unreadable.read.side_effect = _client_error(400, "InvalidRequest", "GetObject")
    with patch.object(encryption_state, "_objects", return_value=unreadable):
        assert encryption_state.sentinel_present(root)
        with pytest.raises(StorageEncryptionError, match="cannot be turned off"):
            encryption_state.ensure_disabled_allowed(root)


def test_an_unreachable_store_is_not_taken_for_encrypted(bucket: Callable[..., tuple[Any, Any]]) -> None:
    _, root = bucket(None)
    broken = MagicMock()
    broken.read.side_effect = ConnectionError("no route")
    with patch.object(encryption_state, "_objects", return_value=broken), pytest.raises(ConnectionError):
        encryption_state.sentinel_present(root)


class _Reads:
    """Object IO whose keyed and plain reads fail as given."""

    def __init__(self, keyed: Exception, plain: Exception) -> None:
        self.keyed, self.plain = keyed, plain

    def __call__(self, _location: Any, *, keyed: bool = True) -> Any:
        io = MagicMock()
        io.read.side_effect = self.keyed if keyed else self.plain
        return io


def test_a_throttled_read_is_not_a_wrong_key(bucket: Callable[..., tuple[Any, Any]]) -> None:
    settings, root = bucket()
    _open(settings)
    _finish(root)
    reads = _Reads(keyed=_client_error(503, "SlowDown", "GetObject"), plain=_client_error(400, "400", "GetObject"))
    with patch.object(encryption_state, "_objects", side_effect=reads):
        with pytest.raises(Exception) as caught:
            encryption_state.read_sentinel(root, KEY)
        assert not isinstance(caught.value, StorageEncryptionError)
        with pytest.raises(Exception) as caught:
            encryption_state.read_marker(root)
        assert not isinstance(caught.value, StorageEncryptionError)


def test_a_startup_check_that_could_not_finish_is_not_a_refusal(bucket: Callable[..., tuple[Any, Any]]) -> None:
    settings, root = bucket()
    _open(settings)
    _finish(root)
    encryption_state.reset_for_tests()
    reads = _Reads(keyed=_client_error(503, "SlowDown", "GetObject"), plain=_client_error(503, "SlowDown"))
    with patch.object(encryption_state, "_objects", side_effect=reads):
        ((_, problem),) = encryption_state.verify_at_startup(settings)
    assert problem is not None and "SlowDown" in problem
    _open(settings)  # the store is back: not refused for the process


def test_an_unchecked_location_gets_the_full_check_on_its_next_open(bucket: Callable[..., tuple[Any, Any]]) -> None:
    """The key was removed and the store was unreachable at startup: the next open still refuses it."""
    settings, root = bucket()
    _open(settings)
    _finish(root)
    encryption_state.reset_for_tests()
    off, _ = bucket(None)
    with patch.object(encryption_state, "sentinel_present", side_effect=ConnectionError("down")):
        ((_, problem),) = encryption_state.verify_at_startup(off)
    assert problem is not None and "down" in problem
    with pytest.raises(StorageUnavailable, match="cannot be turned off"):
        _open(off)


def test_a_missing_state_folder_on_hns_is_not_a_marker(bucket: Callable[..., tuple[Any, Any]]) -> None:
    from azure.core.exceptions import ResourceNotFoundError

    _, root = bucket(None)
    with patch.object(type(root), "iterdir", side_effect=ResourceNotFoundError("PathNotFound")):
        assert encryption_state.marker_listed(root) is False
    with patch.object(type(root), "iterdir", side_effect=ConnectionError("down")), pytest.raises(ConnectionError):
        encryption_state.marker_listed(root)


def test_a_read_only_check_never_enables_encryption(bucket: Callable[..., tuple[Any, Any]]) -> None:
    settings, root = bucket()
    path = clients.open_cloud_uri("s3://docs/", settings, None, error=StorageUnavailable, what="x", check=False)
    encryption_state.check_location(path, settings, thorough=True, create=False)
    assert not (root / ".pincer").exists()
    assert not (root / ".encrypted").exists()


def test_hns_probe_failure_refuses_the_location(bucket: Callable[..., tuple[Any, Any]]) -> None:
    settings, root = bucket()
    root.client.is_hns = lambda _path: True
    root.client.probe_hns = lambda _base: [("write with the key", None), ("rename through Data Lake", "boom")]
    try:
        with pytest.raises(StorageUnavailable, match="failed at step 'rename through Data Lake': boom"):
            _open(settings)
    finally:
        del root.client.is_hns, root.client.probe_hns


def test_locations_cover_a_skills_bucket_outside_the_root(tmp_path: Path) -> None:
    settings = _settings(tmp_path, storage_root="s3://docs/", skills_dir="gs://skills-bucket/team")
    assert encryption_state.locations(settings) == ["s3://docs", "gs://skills-bucket/team"]
    assert encryption_state.guard_location("s3://docs/workspace/uploads", settings) == "s3://docs"
    assert encryption_state.guard_location("gs://skills-bucket/team/x", settings) == "gs://skills-bucket/team/x"
    inside = _settings(tmp_path, storage_root="s3://docs/", skills_dir="s3://docs/skills")
    assert encryption_state.locations(inside) == ["s3://docs"]


# ── Storage browser ──────────────────────────────────────────────────────────


@pytest.fixture
def encrypted_api(bucket: Callable[..., tuple[Any, Any]]) -> Any:
    """The storage API over an encrypted, fully encrypted cloud root."""
    from fastapi.testclient import TestClient

    from pincer.api.auth import current_identity
    from pincer.api.server import create_app
    from pincer.api.storage import storage_browser
    from pincer.services.auth import AuthIdentity

    settings, root = bucket()
    _open(settings)
    _finish(root)
    root.client.encryption_key = KEY
    browser = StorageBrowser.from_settings(settings)
    app = create_app()
    app.dependency_overrides[storage_browser] = lambda: browser
    app.dependency_overrides[current_identity] = lambda: AuthIdentity("alice", "jwt")
    yield TestClient(app), browser, root
    del root.client.encryption_key


def test_an_encrypted_root_serves_its_own_signed_links(encrypted_api: Any) -> None:
    api, browser, root = encrypted_api
    (root / "notes.txt").write_text("plain text")
    assert browser.info()["encrypted"] is True
    link = api.get("/api/storage/presign", params={"key": "notes.txt", "disposition": "inline"}).json()["url"]
    assert link.startswith("/api/storage/signed/")
    res = api.get(link)
    assert res.status_code == 200, res.text
    assert res.text == "plain text"


def test_the_lock_is_hidden_from_listings_and_the_info(encrypted_api: Any) -> None:
    api, _, root = encrypted_api
    (root / "notes.txt").write_text("x")
    listing = api.get("/api/storage/list").json()
    assert [f["name"] for f in listing["files"]] == ["notes.txt"]
    assert listing["folders"] == []
    info = api.get("/api/storage").json()
    assert ".encrypted" not in info["protected"]
    assert ".pincer" not in info["protected"]


@pytest.mark.parametrize("key", [".encrypted", ".pincer/encryption.json", ".pincer/encrypt.lock"])
def test_no_storage_endpoint_exposes_the_lock(encrypted_api: Any, key: str) -> None:
    api, _, root = encrypted_api
    assert api.get("/api/storage/text", params={"key": key}).status_code == 404
    assert api.get("/api/storage/download", params={"key": key}).status_code == 404
    assert api.get("/api/storage/presign", params={"key": key}).status_code == 404
    assert api.get("/api/storage/list", params={"prefix": ".pincer"}).status_code == 404
    assert api.put("/api/storage/text", json={"key": key, "content": "x"}).status_code == 404
    assert api.delete("/api/storage/object", params={"key": key}).status_code == 404
    assert api.delete("/api/storage/object", params={"prefix": ".pincer"}).status_code == 404
    assert api.post("/api/storage/move", json={"from": key, "to": "stolen"}).status_code == 404
    (root / "notes").write_text("n")
    assert api.post("/api/storage/move", json={"from": "notes", "to": key}).status_code == 404
    assert api.post("/api/storage/folders", json={"prefix": ".pincer/x"}).status_code == 404
    up = api.post("/api/storage/upload", params={"prefix": ""}, files={"files": (key, b"x")})
    assert up.status_code == 404
    assert (root / ".encrypted").read_text() == KEY.fingerprint


# ── Encrypting existing data ─────────────────────────────────────────────────


class _Encrypting:
    """Adds the job's two methods to an offline client, tracking state in sets."""

    def __init__(self, client: Any) -> None:
        self.encrypted: set[str] = set()
        self.conflicts: set[str] = set()
        self.broken: set[str] = set()
        client.is_encrypted = lambda path: str(path) in self.encrypted
        client.encrypt_in_place = self._encrypt
        client.encryption_key = KEY
        self.client = client

    def _encrypt(self, path: Any) -> None:
        name = str(path)
        if name in self.conflicts:
            raise KeyConflict(name)
        if name in self.broken:
            raise RuntimeError("provider down")
        self.encrypted.add(name)

    def close(self) -> None:
        del self.client.is_encrypted, self.client.encrypt_in_place, self.client.encryption_key


@pytest.fixture
def seeded(bucket: Callable[..., tuple[Any, Any]]) -> Any:
    settings, root = bucket()
    _open(settings)
    for name in ("a.txt", "b/c.txt", "b/d.txt", "skills/s/SKILL.md"):
        (root / name).write_text(name)
    fake = _Encrypting(root.client)
    yield root, fake
    fake.close()


def test_the_job_encrypts_plain_objects_and_skips_the_rest(seeded: Any) -> None:
    root, fake = seeded
    fake.encrypted.add("s3://docs/a.txt")
    fake.conflicts.add("s3://docs/b/d.txt")
    seen: list[tuple[str, str]] = []
    progress = encrypt_job.encrypt_location(root, on_object=lambda k, o: seen.append((k, o)))
    assert dict(seen) == {
        "a.txt": "already",
        "b/c.txt": "encrypted",
        "b/d.txt": "changed",
        "skills/s/SKILL.md": "encrypted",
    }
    assert progress.state == "complete"
    marker = encryption_state.status(root)
    assert marker is not None and marker["migration"]["state"] == "complete"
    assert marker["migration"]["done"] == 3
    # Every object encrypted: only now does the check file appear.
    assert (root / ".encrypted").read_text() == KEY.fingerprint


def test_a_dry_run_changes_nothing(seeded: Any) -> None:
    root, fake = seeded
    progress = encrypt_job.encrypt_location(root, dry_run=True)
    assert progress.pending == 4
    assert fake.encrypted == set()
    assert not (root / ".encrypted").exists()
    assert encryption_state.status(root)["migration"]["state"] == "pending"


def test_a_failed_object_leaves_the_job_pending(seeded: Any) -> None:
    root, fake = seeded
    fake.broken.add("s3://docs/b/c.txt")
    progress = encrypt_job.encrypt_location(root)
    assert progress.state == "pending"
    assert progress.failed == ["b/c.txt"]
    assert not (root / ".encrypted").exists()
    assert encryption_state.status(root)["migration"]["cursor"] is None


def test_the_job_resumes_after_its_cursor(seeded: Any) -> None:
    root, fake = seeded
    marker, version = encryption_state.read_marker(root)  # type: ignore[misc]
    marker["migration"].update(state="running", cursor="b/c.txt", done=2)
    encryption_state.write_marker(root, marker, if_version=version)
    seen: list[str] = []
    encrypt_job.encrypt_location(root, on_object=lambda k, _o: seen.append(k))
    assert seen == ["b/d.txt", "skills/s/SKILL.md"]
    assert encryption_state.status(root)["migration"]["done"] == 4


def test_the_job_needs_an_encrypting_client(bucket: Callable[..., tuple[Any, Any]]) -> None:
    settings, root = bucket()
    _open(settings)
    with pytest.raises(StorageEncryptionError, match="not opened with a customer key"):
        encrypt_job.encrypt_location(root)


def test_one_lease_at_a_time(seeded: Any) -> None:
    root, _ = seeded
    first, second = encrypt_job.Lease(root), encrypt_job.Lease(root)
    second.owner = "another-host:1:1"
    assert first.acquire()
    assert not second.acquire()
    first.release()
    assert second.acquire()


def test_an_expired_lease_can_be_taken_over(seeded: Any) -> None:
    root, _ = seeded
    stale = encrypt_job.Lease(root)
    stale.owner = "gone:1:1"
    (root / ".pincer" / "encrypt.lock").write_text(json.dumps({"owner": stale.owner, "expires_at": 0}))
    assert encrypt_job.Lease(root).acquire()


def test_a_lease_is_renewed_and_lost_to_a_take_over(seeded: Any) -> None:
    root, _ = seeded
    mine = encrypt_job.Lease(root)
    assert mine.acquire()
    assert mine.refresh()
    held = json.loads((root / ".pincer" / "encrypt.lock").read_text())
    (root / ".pincer" / "encrypt.lock").write_text(json.dumps({"owner": "other:1:1", "expires_at": held["expires_at"]}))
    assert not mine.refresh()


def test_only_the_owner_releases_a_lease(seeded: Any) -> None:
    root, _ = seeded
    mine, theirs = encrypt_job.Lease(root), encrypt_job.Lease(root)
    theirs.owner = "other:1:1"
    assert mine.acquire()
    theirs.release()
    assert (root / ".pincer" / "encrypt.lock").exists()
    mine.release()
    assert not (root / ".pincer" / "encrypt.lock").exists()


def test_a_lost_lease_stops_the_job(seeded: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    root, fake = seeded
    monkeypatch.setattr(encrypt_job, "PROGRESS_EVERY", 1)
    progress = encrypt_job.encrypt_location(root, heartbeat=lambda: False)
    assert progress.state == "running"
    assert len(fake.encrypted) == 1
    assert not (root / ".encrypted").exists()


def test_a_dry_run_before_enabling_writes_no_lock(bucket: Callable[..., tuple[Any, Any]]) -> None:
    _, root = bucket()
    (root / "a.txt").write_text("a")
    fake = _Encrypting(root.client)
    try:
        progress = encrypt_job.encrypt_location(root, dry_run=True)
    finally:
        fake.close()
    assert progress.pending == 1
    assert not (root / ".pincer").exists()


def test_start_runs_the_job_in_the_background_once(seeded: Any, bucket: Callable[..., tuple[Any, Any]]) -> None:
    root, fake = seeded
    settings, _ = bucket()
    thread = encrypt_job.start(settings)
    assert thread is not None
    thread.join(timeout=10)
    assert "s3://docs/a.txt" in fake.encrypted
    assert encryption_state.status(root)["migration"]["state"] == "complete"
    assert encrypt_job.start(settings) is None  # once per process


# ── pincer doctor ────────────────────────────────────────────────────────────


def _doctor(settings: Any) -> Any:
    from pincer.security.doctor import SecurityDoctor

    return SecurityDoctor()._check_storage_encryption(settings)


def test_doctor_skips_without_cloud_storage(tmp_path: Path) -> None:
    assert _doctor(_settings(tmp_path)).status == "skipped"


def test_doctor_warns_a_key_on_local_storage_does_nothing(tmp_path: Path) -> None:
    result = _doctor(_settings(tmp_path, storage_encryption_key=KEY_B64))
    assert result.status == "warning"
    assert "no effect" in result.message


def test_doctor_fails_a_non_tls_endpoint_and_links_the_warning(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        storage_root="s3://docs/",
        storage_encryption_key=KEY_B64,
        storage_s3_endpoint_url="http://minio:9000",
    )
    result = _doctor(settings)
    assert result.status == "critical"
    assert TLS_WARNING_DOC in result.message


def test_doctor_reports_progress_and_completion(seeded: Any, bucket: Callable[..., tuple[Any, Any]]) -> None:
    root, _ = seeded
    settings, _ = bucket()
    pending = _doctor(settings)
    assert pending.status == "warning"
    assert "pending" in pending.message
    encrypt_job.encrypt_location(root)
    done = _doctor(settings)
    assert done.status == "pass"
    assert "complete" in done.message


def test_doctor_never_enables_encryption(bucket: Callable[..., tuple[Any, Any]]) -> None:
    settings, root = bucket()
    result = _doctor(settings)
    assert result.status == "warning"
    assert "enabled on the next start" in result.message
    assert not (root / ".pincer").exists()


def test_doctor_runs_the_hns_probe_once(bucket: Callable[..., tuple[Any, Any]]) -> None:
    settings, root = bucket()
    _open(settings)
    calls: list[Any] = []
    root.client.is_hns = lambda _path: True
    root.client.probe_hns = lambda base: calls.append(base) or [("write with the key", None)]
    try:
        _doctor(settings)
    finally:
        del root.client.is_hns, root.client.probe_hns
    assert len(calls) == 1


def test_doctor_reports_each_hns_probe_failure(bucket: Callable[..., tuple[Any, Any]]) -> None:
    settings, root = bucket()
    root.client.is_hns = lambda _path: True
    root.client.probe_hns = lambda _base: [("write with the key", None), ("rename through Data Lake", "Denied")]
    try:
        result = _doctor(settings)
    finally:
        del root.client.is_hns, root.client.probe_hns
    assert result.status == "critical"
    assert "rename through Data Lake: Denied" in result.message


def test_doctor_fails_encryption_turned_off(bucket: Callable[..., tuple[Any, Any]]) -> None:
    settings, _ = bucket()
    _open(settings)
    encryption_state.reset_for_tests()
    off, _ = bucket(None)
    result = _doctor(off)
    assert result.status == "critical"
    assert "cannot be turned off" in result.message


def test_storage_key_is_a_dataclass_with_no_key_in_repr() -> None:
    assert "raw" not in repr(StorageKey(b"\0" * 32))


# ── pincer storage ───────────────────────────────────────────────────────────


def test_status_shows_a_provider_error_as_a_row(bucket: Callable[..., tuple[Any, Any]], monkeypatch: Any) -> None:
    from typer.testing import CliRunner

    from pincer.cli.main import app

    settings, _ = bucket()
    monkeypatch.setattr("pincer.config.get_settings_relaxed", lambda: settings)
    with patch.object(encryption_state, "check_location", side_effect=_client_error(403, "AccessDenied")):
        result = CliRunner().invoke(app, ["storage", "encryption-status"])
    assert result.exit_code == 0, result.output
    assert "could not check" in result.output


def test_a_dry_run_command_writes_no_lock(bucket: Callable[..., tuple[Any, Any]], monkeypatch: Any) -> None:
    from typer.testing import CliRunner

    from pincer.cli.main import app

    settings, root = bucket()
    (root / "a.txt").write_text("a")
    monkeypatch.setattr("pincer.config.get_settings_relaxed", lambda: settings)
    fake = _Encrypting(root.client)
    try:
        result = CliRunner().invoke(app, ["storage", "encrypt", "--dry-run"])
    finally:
        fake.close()
    assert result.exit_code == 0, result.output
    assert "1 object(s) to encrypt" in result.output
    assert not (root / ".pincer").exists()
