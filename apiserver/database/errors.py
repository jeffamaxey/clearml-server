import re
from contextlib import contextmanager
from functools import wraps
from textwrap import shorten

import dpath
from dpath.exceptions import InvalidKeyName
from elasticsearch import ElasticsearchException
from elasticsearch.helpers import BulkIndexError
from jsonmodels.errors import ValidationError as JsonschemaValidationError
from mongoengine.errors import (
    ValidationError,
    NotUniqueError,
    FieldDoesNotExist,
    InvalidDocumentError,
    LookUpError,
    InvalidQueryError,
)
from pymongo.errors import PyMongoError, NotMasterError

from apiserver.apierrors import errors


class MakeGetAllQueryError(Exception):
    def __init__(self, error, field):
        super(MakeGetAllQueryError, self).__init__(f"{error}: field={field}")
        self.error = error
        self.field = field


class ParseCallError(Exception):
    def __init__(self, msg, **kwargs):
        super(ParseCallError, self).__init__(msg)
        self.params = kwargs


def throws_default_error(err_cls, shorten_width: int = None):
    """
    Used to make functions (Exception, str) -> Optional[str] searching for specialized error messages raise those
    messages in ``err_cls``. If the decorated function does not find a suitable error message,
    the underlying exception is returned.
    :param err_cls: Error class (generated by apierrors)
    """

    def decorator(func):
        @wraps(func)
        def wrapper(self, e, message, **kwargs):
            extra_info = func(self, e, message, **kwargs)
            err = str(e)
            if shorten_width:
                err = shorten(err, shorten_width, placeholder="...")
            raise err_cls(message, err=err, extra_info=extra_info)

        return wrapper

    return decorator


# noinspection RegExpRedundantEscape
class ElasticErrorsHandler(object):
    @classmethod
    def _bulk_meta_error(cls, error):
        try:
            _, err_type = next(dpath.search(error, "*/error/type", yielded=True))
            _, reason = next(dpath.search(error, "*/error/reason", yielded=True))
            if err_type == "cluster_block_exception":
                raise errors.server_error.LowDiskSpace(
                    "metrics, logs and all indexed data is in read-only mode!",
                    reason=re.sub(r"^index\s\[.*?\]\s", "", reason) if reason else ""
                )
            return
        except StopIteration:
            pass

    @classmethod
    @throws_default_error(errors.server_error.DataError, shorten_width=200)
    def bulk_error(cls, e, _, **__):
        if not e.errors:
            return

        # Currently we only handle the first error
        error = e.errors[0]

        cls._bulk_meta_error(error)

        # Else try returning a better error string
        for _, reason in dpath.search(e.errors[0], "*/error/reason", yielded=True):
            return reason


# noinspection RegExpRedundantEscape
class MongoEngineErrorsHandler(object):
    # NotUniqueError
    __not_unique_regex = re.compile(
        r"collection:\s(?P<collection>[\w.]+)\sindex:\s(?P<index>\w+)\sdup\skey:\s{(?P<values>[^\}]+)\}"
    )
    __not_unique_value_regex = re.compile(r':\s"(?P<value>[^"]+)"')
    __id_index = "_id_"
    __index_sep_regex = re.compile(r"_[0-9]+_?")

    # FieldDoesNotExist
    __not_exist_fields_regex = re.compile(r'"{(?P<fields>.+?)}".+?"(?P<document>.+?)"')
    __not_exist_field_regex = re.compile(r"'(?P<field>\w+)'")

    @classmethod
    def validation_error(cls, e: ValidationError, message, **_):
        # Thrown when a document is validated. Documents are validated by default on save and on update
        err_dict = e.errors or {e.field_name: e.message}
        err_dict = {key: str(value) for key, value in err_dict.items()}
        raise errors.bad_request.DataValidationError(message, **err_dict)

    @classmethod
    def not_unique_error(cls, e, message, **_):
        # Thrown when a save/update violates a unique index constraint
        m = cls.__not_unique_regex.search(str(e))
        if not m:
            raise errors.bad_request.ExpectedUniqueData(message, err=str(e))
        values = cls.__not_unique_value_regex.findall(m.group("values"))
        index = m.group("index")
        if index == cls.__id_index:
            fields = "id"
        else:
            fields = cls.__index_sep_regex.split(index)[:-1]
        raise errors.bad_request.ExpectedUniqueData(
            message, **dict(zip(fields, values))
        )

    @classmethod
    def field_does_not_exist(cls, e, message, **kwargs):
        # Strict mode. Unknown fields encountered in loaded document(s)
        field_does_not_exist_cls = kwargs.get(
            "field_does_not_exist_cls", errors.server_error.InconsistentData
        )
        m = cls.__not_exist_fields_regex.search(str(e))
        params = {}
        if m:
            params["document"] = m.group("document")
            if fields := cls.__not_exist_field_regex.findall(m.group("fields")):
                if len(fields) > 1:
                    params["fields"] = f'({", ".join(fields)})'
                else:
                    params["field"] = fields[0]
        raise field_does_not_exist_cls(message, **params)

    @classmethod
    @throws_default_error(errors.server_error.DataError)
    def invalid_document_error(cls, e, message, **_):
        # Reverse_delete_rule used in reference field
        pass

    @classmethod
    def lookup_error(cls, e, message, **_):
        raise errors.bad_request.InvalidFields(
            "probably an invalid field name or unsupported nested field",
            replacement_msg="Lookup error",
            err=str(e),
        )

    @classmethod
    @throws_default_error(errors.bad_request.InvalidRegexError)
    def invalid_regex_error(cls, e, _, **__):
        if e.args and e.args[0] == "unexpected end of regular expression":
            raise errors.bad_request.InvalidRegexError(e.args[0])

    @classmethod
    @throws_default_error(errors.server_error.InternalError)
    def invalid_query_error(cls, e, message, **_):
        pass


@contextmanager
def translate_errors_context(message=None, **kwargs):
    """
    A context manager that translates MongoEngine's and Elastic thrown errors into our apierrors classes,
    with an appropriate message.
    """
    try:
        if message:
            message = f"while {message}"
        yield True
    except ValidationError as e:
        MongoEngineErrorsHandler.validation_error(e, message, **kwargs)
    except NotUniqueError as e:
        MongoEngineErrorsHandler.not_unique_error(e, message, **kwargs)
    except FieldDoesNotExist as e:
        MongoEngineErrorsHandler.field_does_not_exist(e, message, **kwargs)
    except InvalidDocumentError as e:
        MongoEngineErrorsHandler.invalid_document_error(e, message, **kwargs)
    except LookUpError as e:
        MongoEngineErrorsHandler.lookup_error(e, message, **kwargs)
    except re.error as e:
        MongoEngineErrorsHandler.invalid_regex_error(e, message, **kwargs)
    except InvalidQueryError as e:
        MongoEngineErrorsHandler.invalid_query_error(e, message, **kwargs)
    except PyMongoError as e:
        raise errors.server_error.InternalError(message, err=str(e))
    except NotMasterError as e:
        raise errors.server_error.InternalError(message, err=str(e))
    except MakeGetAllQueryError as e:
        raise errors.bad_request.ValidationError(e.error, field=e.field)
    except ParseCallError as e:
        raise errors.bad_request.FieldsValueError(e.args[0], **e.params)
    except JsonschemaValidationError as e:
        if len(e.args) >= 2:
            raise errors.bad_request.ValidationError(e.args[0], reason=e.args[1])
        raise errors.bad_request.ValidationError(e.args[0])
    except BulkIndexError as e:
        ElasticErrorsHandler.bulk_error(e, message, **kwargs)
    except ElasticsearchException as e:
        raise errors.server_error.DataError(e, message, **kwargs)
    except InvalidKeyName:
        raise errors.server_error.DataError("invalid empty key encountered in data")
    except Exception as ex:
        raise
