"""Collection metadata: the ``/collections``, ``/queryables``, and ``/schema`` documents.

Each type carries the members it names as typed fields and every other member in a
read-only ``extra``, so ``from_dict(doc).to_dict() == doc`` (L-04, D-33).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, Self, cast

from water_ogcapi._results import Link, ReadOnlyDict, as_str, empty_extra, from_members, to_members

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from water_ogcapi._results import Members

__all__ = [
    "Collection",
    "Collections",
    "Extent",
    "Property",
    "Schema",
    "SpatialExtent",
    "TemporalExtent",
]


def _list_of[T](value: object, parse: Callable[[Any], T | None]) -> tuple[T, ...] | None:
    # One unparsable item rejects the list, which then stays whole in ``extra``.
    if not isinstance(value, list):
        return None
    items = [parse(item) for item in cast("list[Any]", value)]
    return None if any(item is None for item in items) else cast("tuple[T, ...]", tuple(items))


def _strs(value: object) -> tuple[str, ...] | None:
    return _list_of(value, as_str)


def _number(value: object) -> float | None:
    is_number = isinstance(value, int | float) and not isinstance(value, bool)
    return cast("float", value) if is_number else None


def _bbox(value: object) -> tuple[float, ...] | None:
    numbers = _list_of(value, _number)
    return numbers if numbers is not None and len(numbers) in {4, 6} else None


def _interval(value: object) -> tuple[str | None, str | None] | None:
    # An open end is null, so this cannot go through _list_of.
    if not isinstance(value, list) or len(cast("list[Any]", value)) != 2:
        return None
    start, end = cast("list[Any]", value)
    if all(item is None or isinstance(item, str) for item in (start, end)):
        return (start, end)
    return None


def _object[T](parse: Callable[[Mapping[str, Any]], T]) -> Callable[[Any], T | None]:
    def parse_object(value: object) -> T | None:
        return parse(cast("dict[str, Any]", value)) if isinstance(value, dict) else None

    return parse_object


def _link(value: object) -> Link | None:
    is_link = isinstance(value, dict) and isinstance(cast("dict[str, Any]", value).get("href"), str)
    return Link.from_dict(cast("dict[str, Any]", value)) if is_link else None


def _links(value: object) -> tuple[Link, ...] | None:
    return _list_of(value, _link)


@dataclass(frozen=True, slots=True)
class SpatialExtent:
    """The ``spatial`` member of a collection's extent.

    Parameters
    ----------
    bbox : tuple of tuple of float, optional
        Every bounding box the server sent, in order: 4 or 6 numbers each. The first
        covers the whole collection.
    crs : str, optional
        CRS of the boxes.
    extra : mapping of str to Any, optional
        Every other member.
    """

    bbox: tuple[tuple[float, ...], ...] | None = None
    crs: str | None = None
    extra: Mapping[str, Any] = field(default_factory=empty_extra, hash=False)

    _MEMBERS: ClassVar[Members] = {
        "bbox": ("bbox", lambda value: _list_of(value, _bbox)),
        "crs": ("crs", as_str),
    }

    @classmethod
    def from_dict(cls, doc: Mapping[str, Any]) -> Self:
        """Parse the member."""
        return cls(**from_members(doc, cls._MEMBERS))

    def to_dict(self) -> dict[str, Any]:
        """Return the member as the server sent it."""
        return to_members(self, self._MEMBERS, self.extra)


@dataclass(frozen=True, slots=True)
class TemporalExtent:
    """The ``temporal`` member of a collection's extent.

    Parameters
    ----------
    interval : tuple of (str or None, str or None), optional
        Every interval the server sent, in order. ``None`` marks an open end.
    trs : str, optional
        Temporal reference system.
    extra : mapping of str to Any, optional
        Every other member.
    """

    interval: tuple[tuple[str | None, str | None], ...] | None = None
    trs: str | None = None
    extra: Mapping[str, Any] = field(default_factory=empty_extra, hash=False)

    _MEMBERS: ClassVar[Members] = {
        "interval": ("interval", lambda value: _list_of(value, _interval)),
        "trs": ("trs", as_str),
    }

    @classmethod
    def from_dict(cls, doc: Mapping[str, Any]) -> Self:
        """Parse the member."""
        return cls(**from_members(doc, cls._MEMBERS))

    def to_dict(self) -> dict[str, Any]:
        """Return the member as the server sent it."""
        return to_members(self, self._MEMBERS, self.extra)


@dataclass(frozen=True, slots=True)
class Extent:
    """A collection's ``extent``.

    Parameters
    ----------
    spatial : SpatialExtent, optional
        Bounding boxes.
    temporal : TemporalExtent, optional
        Time intervals.
    extra : mapping of str to Any, optional
        Every other member.
    """

    spatial: SpatialExtent | None = None
    temporal: TemporalExtent | None = None
    extra: Mapping[str, Any] = field(default_factory=empty_extra, hash=False)

    _MEMBERS: ClassVar[Members] = {
        "spatial": ("spatial", _object(SpatialExtent.from_dict)),
        "temporal": ("temporal", _object(TemporalExtent.from_dict)),
    }

    @classmethod
    def from_dict(cls, doc: Mapping[str, Any]) -> Self:
        """Parse the member."""
        return cls(**from_members(doc, cls._MEMBERS))

    def to_dict(self) -> dict[str, Any]:
        """Return the member as the server sent it."""
        return to_members(self, self._MEMBERS, self.extra)


@dataclass(frozen=True, slots=True)
class Collection:
    """One collection, from ``/collections`` or ``/collections/{id}``.

    A field is ``None`` when the server left its member out or sent it with a shape
    the field cannot hold, such as a null; that member then stays in ``extra``.

    Parameters
    ----------
    id : str
        Collection identifier, the path segment in its URLs.
    title : str, optional
        Human-readable title.
    description : str, optional
        Long description.
    keywords : tuple of str, optional
        Keywords.
    links : tuple of Link, optional
        Links, ``href`` redacted.
    extent : Extent, optional
        Spatial and temporal extent.
    item_type : str, optional
        ``itemType``, such as ``feature`` or ``record``.
    crs : tuple of str, optional
        Every CRS the server can return features in.
    storage_crs : str, optional
        ``storageCrs``, the CRS the server stores the features in.
    extra : mapping of str to Any, optional
        Every other member.
    """

    id: str
    title: str | None = None
    description: str | None = None
    keywords: tuple[str, ...] | None = None
    links: tuple[Link, ...] | None = None
    extent: Extent | None = None
    item_type: str | None = None
    crs: tuple[str, ...] | None = None
    storage_crs: str | None = None
    extra: Mapping[str, Any] = field(default_factory=empty_extra, hash=False)

    _MEMBERS: ClassVar[Members] = {
        "id": ("id", as_str),
        "title": ("title", as_str),
        "description": ("description", as_str),
        "keywords": ("keywords", _strs),
        "links": ("links", _links),
        "extent": ("extent", _object(Extent.from_dict)),
        "itemType": ("item_type", as_str),
        "crs": ("crs", _strs),
        "storageCrs": ("storage_crs", as_str),
    }

    @classmethod
    def from_dict(cls, doc: Mapping[str, Any]) -> Self:
        """Parse a collection document.

        Raises
        ------
        ValueError
            If ``doc`` has no string ``id``.
        """
        return cls(**from_members(doc, cls._MEMBERS, required="id"))

    def to_dict(self) -> dict[str, Any]:
        """Return the document as the server sent it, ``href`` redacted in parsed links."""
        return to_members(self, self._MEMBERS, self.extra)


def _collection(value: object) -> Collection:
    if not isinstance(value, dict):
        msg = "each entry of 'collections' must be an object"
        raise ValueError(msg)  # noqa: TRY004
    return Collection.from_dict(cast("dict[str, Any]", value))


@dataclass(frozen=True, slots=True)
class Collections:
    """The ``/collections`` document.

    Parameters
    ----------
    collections : tuple of Collection, optional
        Every collection, in the server's order.
    links : tuple of Link, optional
        Links of the document itself.
    extra : mapping of str to Any, optional
        Every other member.
    """

    collections: tuple[Collection, ...] | None = None
    links: tuple[Link, ...] | None = None
    extra: Mapping[str, Any] = field(default_factory=empty_extra, hash=False)

    _MEMBERS: ClassVar[Members] = {
        "collections": ("collections", lambda value: _list_of(value, _collection)),
        "links": ("links", _links),
    }

    @classmethod
    def from_dict(cls, doc: Mapping[str, Any]) -> Self:
        """Parse the document.

        Raises
        ------
        ValueError
            If an entry of ``collections`` is not an object with a string ``id``.
        """
        return cls(**from_members(doc, cls._MEMBERS))

    def to_dict(self) -> dict[str, Any]:
        """Return the document as the server sent it, ``href`` redacted in parsed links."""
        return to_members(self, self._MEMBERS, self.extra)


@dataclass(frozen=True, slots=True)
class Property:
    """One property of a ``/queryables`` or ``/schema`` document, a JSON Schema.

    Parameters
    ----------
    type : str, optional
        JSON type, such as ``string`` or ``integer``.
    title : str, optional
        Human-readable name.
    description : str, optional
        Long description.
    format : str, optional
        Format, such as ``date-time`` or ``geometry-any``.
    role : str, optional
        ``x-ogc-role``, such as ``id`` or ``primary-geometry``.
    extra : mapping of str to Any, optional
        Every other member, such as ``enum`` or ``example``.
    """

    type: str | None = None
    title: str | None = None
    description: str | None = None
    format: str | None = None
    role: str | None = None
    extra: Mapping[str, Any] = field(default_factory=empty_extra, hash=False)

    _MEMBERS: ClassVar[Members] = {
        "type": ("type", as_str),
        "title": ("title", as_str),
        "description": ("description", as_str),
        "format": ("format", as_str),
        "x-ogc-role": ("role", as_str),
    }

    @classmethod
    def from_dict(cls, doc: Mapping[str, Any]) -> Self:
        """Parse the property."""
        return cls(**from_members(doc, cls._MEMBERS))

    def to_dict(self) -> dict[str, Any]:
        """Return the property as the server sent it."""
        return to_members(self, self._MEMBERS, self.extra)


def _properties(value: object) -> ReadOnlyDict[Property] | None:
    if not isinstance(value, dict):
        return None
    parse = _object(Property.from_dict)
    parsed = {name: parse(item) for name, item in cast("dict[str, Any]", value).items()}
    if any(item is None for item in parsed.values()):
        return None
    return ReadOnlyDict(cast("dict[str, Property]", parsed))


@dataclass(frozen=True, slots=True)
class Schema:
    """A ``/queryables`` or ``/schema`` document.

    ``/queryables`` lists the properties a query can filter and sort on, ``/schema``
    the properties a feature carries.

    Parameters
    ----------
    title : str, optional
        Human-readable title.
    properties : mapping of str to Property, optional
        Every property by name, in the server's order. Read-only.
    extra : mapping of str to Any, optional
        Every other member, such as ``$id`` and ``$schema``.
    """

    title: str | None = None
    properties: Mapping[str, Property] | None = field(default=None, hash=False)
    extra: Mapping[str, Any] = field(default_factory=empty_extra, hash=False)

    _MEMBERS: ClassVar[Members] = {
        "title": ("title", as_str),
        "properties": ("properties", _properties),
    }

    @classmethod
    def from_dict(cls, doc: Mapping[str, Any]) -> Self:
        """Parse the document."""
        return cls(**from_members(doc, cls._MEMBERS))

    def to_dict(self) -> dict[str, Any]:
        """Return the document as the server sent it."""
        return to_members(self, self._MEMBERS, self.extra)
