"""schema.org validation rules (based on Google's structured-data documentation) and helpers.

``required`` = needed for Google rich-result eligibility; ``recommended`` = improves it.
Types Google has no rich result for (Organization, Service, WebSite) only have recommendations.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

ANY_OF = "|"  # "offers|review|aggregateRating" = at least one of these


@dataclass(frozen=True)
class TypeRule:
    required: tuple[str, ...] = ()
    recommended: tuple[str, ...] = ()


RULES: dict[str, TypeRule] = {
    "Organization": TypeRule((), ("name", "url", "logo", "sameAs")),
    "LocalBusiness": TypeRule(("name", "address"), ("telephone", "url", "image", "openingHoursSpecification",
                                                     "geo", "priceRange")),
    "WebSite": TypeRule((), ("name", "url")),
    "Article": TypeRule((), ("headline", "image", "datePublished", "dateModified", "author")),
    "Product": TypeRule(("name", "offers|review|aggregateRating"), ("image", "description", "brand", "sku")),
    "Offer": TypeRule(("price|priceSpecification", "priceCurrency|priceSpecification"), ("availability", "url")),
    "AggregateOffer": TypeRule(("lowPrice", "priceCurrency"), ("highPrice", "offerCount")),
    "BreadcrumbList": TypeRule(("itemListElement",), ()),
    "ListItem": TypeRule(("position", "name|item"), ()),
    "FAQPage": TypeRule(("mainEntity",), ()),
    "Question": TypeRule(("name", "acceptedAnswer"), ()),
    "Answer": TypeRule(("text",), ()),
    "AggregateRating": TypeRule(("ratingValue", "ratingCount|reviewCount"), ("bestRating",)),
    "Review": TypeRule(("author", "reviewRating"), ("datePublished",)),
    "Rating": TypeRule(("ratingValue",), ("bestRating",)),
    "VideoObject": TypeRule(("name", "thumbnailUrl", "uploadDate"), ("description", "contentUrl|embedUrl",
                                                                      "duration")),
    "Event": TypeRule(("name", "startDate", "location"), ("endDate", "eventStatus", "image", "description",
                                                           "offers", "organizer")),
    "Service": TypeRule((), ("name", "provider", "areaServed", "description")),
    "Person": TypeRule(("name",), ()),
    "PostalAddress": TypeRule((), ("streetAddress", "addressLocality", "postalCode", "addressCountry")),
}

ALIASES = {"BlogPosting": "Article", "NewsArticle": "Article", "TechArticle": "Article", "Report": "Article",
           "ScholarlyArticle": "Article", "Corporation": "Organization", "NGO": "Organization",
           "OnlineStore": "Organization", "EducationalOrganization": "Organization", "ProductGroup": "Product",
           "IndividualProduct": "Product", "Course": None}

LOCAL_SUBTYPES = {
    "LocalBusiness", "Store", "Restaurant", "FoodEstablishment", "Dentist", "Physician", "MedicalClinic",
    "MedicalBusiness", "AutomotiveBusiness", "AutoRepair", "AutoDealer", "LegalService", "Attorney",
    "HomeAndConstructionBusiness", "Plumber", "Electrician", "HVACBusiness", "RealEstateAgent",
    "SportsActivityLocation", "HealthAndBeautyBusiness", "BeautySalon", "HairSalon", "DaySpa",
    "ProfessionalService", "FinancialService", "AccountingService", "LodgingBusiness", "Hotel",
    "EntertainmentBusiness", "ChildCare", "EmergencyService", "GovernmentOffice", "Library",
    "TravelAgency", "CafeOrCoffeeShop", "Bakery", "BarOrPub", "ExerciseGym", "SportsClub",
}
SINGLETONS = {"Organization", "LocalBusiness", "WebSite", "BreadcrumbList", "FAQPage"}
NESTED = {"offers": "Offer", "itemListElement": "ListItem", "mainEntity": "Question", "acceptedAnswer": "Answer",
          "aggregateRating": "AggregateRating", "review": "Review", "reviewRating": "Rating",
          "address": "PostalAddress"}


def types_of(item: dict) -> list[str]:
    t = item.get("@type") or item.get("type")
    vals = t if isinstance(t, list) else [t] if t else []
    out = []
    for v in vals:
        v = str(v).rsplit("/", 1)[-1].rsplit(":", 1)[-1]  # "http://schema.org/Product", "schema:Product"
        out.append(v)
    return out


def canonical_type(t: str) -> str | None:
    if t in RULES:
        return t
    if t in ALIASES:
        return ALIASES[t]
    if t in LOCAL_SUBTYPES or t.endswith("Store") or t.endswith("Business"):
        return "LocalBusiness"
    return None


def _present(item: dict, prop: str) -> bool:
    v = item.get(prop)
    if v is None:
        return False
    if isinstance(v, str):
        return bool(v.strip())
    if isinstance(v, (list, dict)):
        return bool(v)
    return True


def has_prop(item: dict, spec: str) -> bool:
    return any(_present(item, p) for p in spec.split(ANY_OF))


@dataclass
class Validation:
    type: str
    missing_required: list[str] = field(default_factory=list)
    missing_recommended: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing_required and not self.errors


def validate(item: dict, path: str = "", is_root: bool = True, jsonld: bool = True) -> list[Validation]:
    """Validate one item (and known nested items). Returns one Validation per known typed node."""
    out: list[Validation] = []
    ts = types_of(item)
    if is_root and not ts:
        v = Validation(type="(none)")
        v.errors.append("Item has no @type")
        return [v]
    if is_root and jsonld:
        ctx = item.get("@context")
        ctx_s = str(ctx).lower() if ctx is not None else ""
        if not ctx:
            v = Validation(type="/".join(ts))
            v.errors.append("Missing @context (use \"https://schema.org\")")
            out.append(v)
        elif "schema.org" not in ctx_s:
            v = Validation(type="/".join(ts))
            v.errors.append(f"@context is not schema.org: {str(ctx)[:80]}")
            out.append(v)
    for t in ts:
        ct = canonical_type(t)
        if not ct:
            continue
        rule = RULES[ct]
        v = Validation(type=f"{path}{t}")
        v.missing_required = [p for p in rule.required if not has_prop(item, p)]
        v.missing_recommended = [p for p in rule.recommended if not has_prop(item, p)]
        out.append(v)
    for prop, child_type in NESTED.items():
        val = item.get(prop)
        children = val if isinstance(val, list) else [val] if isinstance(val, dict) else []
        for i, child in enumerate(children[:50]):
            if not isinstance(child, dict):
                continue
            if not types_of(child):
                child = {**child, "@type": child_type}
            sub = validate(child, f"{path}{prop}[{i}].", is_root=False, jsonld=jsonld)
            out.extend(sub)
    if "BreadcrumbList" in ts:
        items = item.get("itemListElement")
        if isinstance(items, list) and len(items) < 2:
            bad = Validation(type=f"{path}BreadcrumbList")
            bad.errors.append("BreadcrumbList should have at least 2 items")
            out.append(bad)
    return out


def is_valid_jsonld(obj: dict) -> tuple[bool, list[str]]:
    """Code-level validation of generated JSON-LD before it is shown as a fix."""
    import json
    problems: list[str] = []
    try:
        json.loads(json.dumps(obj))
    except (TypeError, ValueError) as e:
        problems.append(f"Not serialisable: {e}")
    for v in validate(obj):
        problems += v.errors
        problems += [f"{v.type}: missing required {p}" for p in v.missing_required]
    return (not problems, problems)


def item_name(item: dict) -> Any:
    return item.get("name") or item.get("headline")
