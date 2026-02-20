"""InvenioRDM REST API models."""

from typing import List, Dict, Optional, Any, Literal

from pydantic import BaseModel


class Identifier(BaseModel):
    """
    Identifier of a person or organization.

    Attributes:
        scheme (str): The identifier scheme.
        identifier (str): Actual value of the identifier.
    """

    scheme: str
    identifier: str


class PersonOrganization(BaseModel):
    """
    A person or an organization.

    Attributes:
        type (str): The type of name. Either personal or organizational.
        given_name (Optional[str]): Given name(s).
        family_name (Optional[str]): Family name.
        name (Optional[str]): The full name of the organisation.
            For a person, this field is generated from given_name and family_name.
        identifiers (Optional[List[Identifier]]): Person or organisation identifiers.
    """

    type: Literal["personal", "organizational"]
    given_name: Optional[str] = None
    family_name: Optional[str] = None
    name: Optional[str] = None
    identifiers: Optional[List[Identifier]] = None


class Affiliation(BaseModel):
    """
    Affiliation of a person associated with a record.

    Attributes:
        id (Optional[str]): The organizational or institutional id from the controlled vocabulary.
        name (Optional[str]): The name of the organisation or institution.
    """

    id: Optional[str] = None
    name: Optional[str] = None


class Role(BaseModel):
    """
    A role of the person or organisation associated with a record.

    Attributes:
        id (str): The role's controlled vocabulary identifier.
    """

    id: str


class Creator(BaseModel):
    """
    Persons or organisations that should be credited for a resource described by the record.

    Attributes:
        person_or_org (PersonOrganization): The person or organization.
        role (Optional[Role]): The role of the person or organisation selected from a customizable controlled vocabulary.
        affiliations (Optional[List[Affiliation]]): Affilations if person_or_org.type is personal.
    """

    person_or_org: PersonOrganization
    role: Optional[Role] = None
    affiliations: Optional[List[Affiliation]] = None


class Contributor(BaseModel):
    """
    The organisations or persons responsible for collecting, managing,
        distributing, or otherwise contributing to the development of the resource.

    Attributes:
        person_or_org (PersonOrganization): The person or organization.
        role (Role): The role of the person or organisation selected from a customizable controlled vocabulary.
        affiliations (Optional[List[Affiliation]]): Affilations if person_or_org.type is personal.
    """

    person_or_org: PersonOrganization
    role: Role
    affiliations: Optional[List[Affiliation]] = None


class ResourceType(BaseModel):
    """
    The type of the resource described by the record.

    Attributes:
        id (str): The resource type id from the controlled vocabulary.
    """

    id: str


class License(BaseModel):
    """
    Rights management statement for a resource.

    Attributes:
        id (str): A license identifier value.
    """

    id: str


class Language(BaseModel):
    """
    The language of a resource.

    Attributes:
        id (str): The ISO-639-3 language code.
    """

    id: str


class DateType(BaseModel):
    """
    A date type.

    Attributes:
        id (str): Date type id from the controlled vocabulary.
        title (Optional[str]): The corresponding localized human readable label.
    """

    id: Literal[
        "accepted",
        "available",
        "collected",
        "copyrighted",
        "created",
        "issued",
        "other",
        "submitted",
        "updated",
        "valid",
        "withdrawn",
    ]
    title: Optional[str] = None


class Date(BaseModel):
    """
    Date relevant to a resource.

    Attributes:
        date (str): A date or time interval.
        type (DateType): The type of date.
        description (Optional[str]): Free text, specific information about the date.
    """

    date: str
    type: DateType
    description: Optional[str] = None


class Funder(BaseModel):
    """
    A funding provider.

    Attributes:
        id (Optional[str]): The funder id from the controlled vocabulary.
        name (Optional[str]): The name of the funder.
    """

    id: Optional[str] = None
    name: Optional[str] = None


class AwardTitle(BaseModel):
    """
    The localized title of the award.

    Attributes:
        en (str): The english title of an award.
    """

    en: str


class Award(BaseModel):
    """
    An award (grant) sponsored by a funder.

    Attributes:
        id (Optional[str]): The award id from the controlled vocabulary.
        title (Optional[AwardTitle]): The localized title of the award.
        numer (Optional[str]): The code assigned by the funder to a sponsored award (grant).
        identifiers (Optional[List[Identifier]]): Identifiers for the award.
    """

    id: Optional[str] = None
    title: Optional[AwardTitle] = None
    number: Optional[str] = None
    identifiers: Optional[List[Identifier]] = None


class Funding(BaseModel):
    """
    Information about financial support (funding) for the resource being registered.

    Attributes:
        funder (Optional[Funder]): The organisation of the funding provider.
        award (Optional[Award]): The award (grant) sponsored by the funder.
    """

    funder: Optional[Funder] = None
    award: Optional[Award] = None


class Subject(BaseModel):
    """
    Subject, keyword, classification code, or key phrase describing the resource.

    Attributes:
        id (Optional[str]): The identifier of the subject from the controlled vocabulary.
        subject (Optional[str]): A custom keyword.
    """

    id: Optional[str] = None
    subject: Optional[str] = None


class RelatedIdentifier(BaseModel):
    """
    Identifiers of related resources (for citations and related works).
    
    This allows you to link your dataset to papers, datasets, software, or other
    resources. Used for citing papers, linking to related datasets, etc.
    
    Attributes:
        identifier: The identifier value (e.g., DOI, URL, arXiv ID)
        scheme: The identifier scheme (e.g., "doi", "url", "arxiv", "isbn", "pmid")
        relation_type: The relationship to this record (e.g., "cites", "isSupplementTo", "references")
        resource_type: Optional type of the related resource
        
    Common relation_type values:
        - "cites": This record cites the related resource
        - "references": This record references the related resource  
        - "isSupplementTo": This record supplements the related resource
        - "isSupplementedBy": This record is supplemented by the related resource
        - "isPartOf": This record is part of the related resource
        - "hasPart": This record has the related resource as a part
        - "isDerivedFrom": This record is derived from the related resource
        
    Common scheme values:
        - "doi": Digital Object Identifier
        - "url": Web URL
        - "arxiv": arXiv identifier
        - "isbn": ISBN book identifier
        - "pmid": PubMed ID
        - "handle": Handle identifier
    """
    
    identifier: str
    scheme: str
    relation_type: str
    resource_type: Optional[ResourceType] = None


class Community(BaseModel):
    """
    A community associated with a record.

    Attributes:
        id (str): Community identifier.
    """

    id: str


class Metadata(BaseModel):
    """
    The metadata of a draft record.
    """

    resource_type: ResourceType
    title: str
    creators: List[Creator]
    publication_date: str
    rights: Optional[List[License]] = None
    description: Optional[str] = None
    contributors: Optional[List[Contributor]] = None
    languages: Optional[List[Language]] = None
    dates: Optional[List[Date]] = None
    version: Optional[str] = None
    publisher: Optional[str] = None
    funding: Optional[List[Funding]] = None
    subjects: Optional[List[Subject]] = None
    communities: Optional[List[Community]] = None
    related_identifiers: Optional[List[RelatedIdentifier]] = None
    references: Optional[List[str]] = None
    custom_fields: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        """
        Converts the model to a JSON-compatible dictionary.
        """
        return self.model_dump(exclude_none=True)

    def to_json(self) -> str:
        """
        Converts the model to a JSON string.
        """
        return self.model_dump_json(exclude_none=True)
