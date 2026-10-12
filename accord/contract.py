"""Both parties approve this complete, immutable application mandate."""
TERMS_VERSION = "accord-single-capture-v1"
MANDATE = (
    "The supplier is the PayPal merchant and authorizes ACCORD, using the supplier's "
    "merchant application, to initiate one final USD 750.00 capture of this deal's "
    "authorization only after the backend accepts the approved PO and lot evidence, "
    "the delivered material grade meets the frozen requirement, and the buyer acknowledges "
    "delivery. The buyer approves these requirements and this conditional capture process. "
    "Capture initiation must occur within 48 hours of PayPal's authorization creation time. "
    "Missing or conflicting evidence requires buyer interpretation review. A passing decision "
    "does not guarantee PayPal capture or settlement succeeds. ACCORD may request a void "
    "on supplier cancellation or deadline expiry only when no capture has been dispatched. "
    "Uncertain financial outcomes require reconciliation. An authenticated upload records "
    "provenance; it does not establish that a certificate is truthful."
)


def policy(po="PO-001",lot="LOT-001"):
    return {"amount":"750.00","currency":"USD","material_grade":"316L",
            "purchase_order":po,"lot":lot,"terms_version":TERMS_VERSION,"mandate":MANDATE,
            "capture_initiation_hours":48}
