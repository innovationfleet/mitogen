from typing import *

class Base:
    # FIXME These types are convenient lies, they're really FieldAttribute
    _connection          = ... # type: str
    _port                = ... # type: int
    _remote_user         = ... # type: str
    _vars                = ... # type: dict
    _environment         = ... # type: list
    _no_log              = ... # type: bool
    _always_run          = ... # type: bool
    _run_once            = ... # type: bool
    _ignore_errors       = ... # type: bool
    _check_mode          = ... # type: bool
    _any_errors_fatal     = ... # type: bool
    DEPRECATED_ATTRIBUTES = ... # type: List[str]

    _loader = ... # type: Optional[Any]
    _variable_manager = ... # type: Optional[Any]
    _validated = ... # type: bool
    _squashed  = ... # type: bool
    _finalized = ... # type: bool
    _uuid = ... # type: str
    _attributes = ... # type: dict
    vars = ... # type: dict

    _ds = ... # type: Optional[Any]
    def __init__(self) -> None: ...
    def dump_me(self, depth: int = ...) -> None: ...
    def preprocess_data(self, ds: Any) -> Any: ...
    def load_data(self, ds: Any, variable_manager: Optional[Any] = ..., loader: Optional[Any] = ...) -> Base: ...
    def get_ds(self) -> Optional[Any]: ...
    def get_loader(self) -> Optional[Any]: ...
    def _validate_attributes(self, ds: Any) -> None: ...
    def validate(self, all_vars: dict = ...) -> None: ...
    def squash(self) -> None: ...
    def copy(self) -> Base: ...
    def post_validate(self, templar: Any) -> None: ...
    def _load_vars(self, attr: Any, ds: Any) -> dict: ...
    def dump_attrs(self) -> dict: ...
    def from_attrs(self, attrs: dict) -> None: ...
    def serialize(self) -> dict: ...
    def deserialize(self, data: dict) -> None: ...
