"""Transparent text rules for the initial Peugeot market catalogue."""

import re
from dataclasses import dataclass
from decimal import Decimal

from vehicles.models import Listing


@dataclass(frozen=True)
class Classification:
    make: str
    model: str
    generation: str
    facelift_status: str
    engine_family: str
    transmission_category: str
    confidence: Decimal
    notes: str


def _contains(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def classify_text(*, make: str, model: str, title: str, transmission: str) -> Classification:
    combined = " ".join((model, title, transmission)).strip()
    normalized_make = "Peugeot" if make.strip().casefold() == "peugeot" else make.strip()
    model_match = re.search(r"\b(2008|3008)\b", combined)
    normalized_model = model_match.group(1) if model_match else model.strip()

    engine_family = ""
    if _contains(combined, (r"\b1[.,]5\s*(?:blue)?hdi\b", r"\bbluehdi\s*130\b", r"\b1[.,]5\s*hdi\s*130\b")):
        engine_family = "1.5 BlueHDi"
    elif _contains(combined, (r"\b1[.,]2\s*puretech\b", r"\bpuretech\s*130\b")):
        engine_family = "1.2 PureTech"

    transmission_category = Listing.TransmissionCategory.UNKNOWN
    if _contains(combined, (r"\beat8\b", r"\bbva8\b", r"\bautomatic(?:a)?\b")):
        transmission_category = Listing.TransmissionCategory.AUTOMATIC
    elif _contains(combined, (r"\bbvm6\b", r"\bmanual\b")):
        transmission_category = Listing.TransmissionCategory.MANUAL

    generation = ""
    facelift_status = Listing.FaceliftStatus.UNKNOWN
    notes = []
    if normalized_make == "Peugeot" and normalized_model == "2008":
        if _contains(combined, (r"\b2008\s+(?:ii|2)\b", r"\bsecond[- ]generation\b", r"\b2nd[- ]generation\b", r"\bmk2\b")):
            generation = "II"
            facelift_status = Listing.FaceliftStatus.NOT_APPLICABLE
        elif _contains(combined, (r"\b2008\s+(?:i|1)\b", r"\bfirst[- ]generation\b", r"\b1st[- ]generation\b", r"\bmk1\b")):
            generation = "I"
            facelift_status = Listing.FaceliftStatus.NOT_APPLICABLE
        else:
            notes.append("Peugeot 2008 generation is not explicit in source text.")
    elif normalized_make == "Peugeot" and normalized_model == "3008":
        if _contains(combined, (r"\b3008\s+(?:ii|2)\b", r"\bsecond[- ]generation\b", r"\b2nd[- ]generation\b", r"\bmk2\b")):
            generation = "II"
        if _contains(combined, (r"\bpre[- ]?facelift\b", r"\bphase\s*1\b")):
            facelift_status = Listing.FaceliftStatus.PRE_FACELIFT
        elif _contains(combined, (r"\bfacelift\b", r"\brestyl(?:e|ed|ing)\b", r"\bphase\s*2\b")):
            facelift_status = Listing.FaceliftStatus.FACELIFT
        else:
            notes.append("Facelift status is not explicit; registration year was not used.")

    recognized = sum(bool(value) for value in (generation, engine_family))
    recognized += transmission_category != Listing.TransmissionCategory.UNKNOWN
    if normalized_model == "3008":
        recognized += facelift_status != Listing.FaceliftStatus.UNKNOWN
    confidence = Decimal("0.95") if recognized >= 4 else Decimal("0.80") if recognized >= 3 else Decimal("0.50")
    return Classification(
        normalized_make, normalized_model, generation, facelift_status,
        engine_family, transmission_category, confidence, " ".join(notes),
    )


def classify_listing(listing: Listing, *, save: bool = True) -> Listing:
    """Apply automatic rules unless a user has manually classified the listing."""
    if listing.classification_manually_overridden:
        return listing
    result = classify_text(
        make=listing.make, model=listing.model, title=listing.title,
        transmission=listing.transmission,
    )
    listing.generation = result.generation
    listing.facelift_status = result.facelift_status
    listing.engine_family = result.engine_family
    listing.transmission_category = result.transmission_category
    listing.classification_confidence = result.confidence
    listing.classification_notes = result.notes
    listing.classification_method = (
        Listing.ClassificationMethod.RULE_BASED
        if any((result.generation, result.engine_family))
        else Listing.ClassificationMethod.UNKNOWN
    )
    if save:
        listing.save(update_fields=(
            "generation", "facelift_status", "engine_family", "transmission_category",
            "classification_confidence", "classification_notes", "classification_method", "updated_at",
        ))
    return listing
