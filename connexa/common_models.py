"""Common Models — the normalized schemas per category that every connector maps onto.
Mirrors the "one model per category + discover-before-write" contract."""
from typing import Any

TICKET_STATUSES = ["open", "pending", "draft", "in_progress", "resolved", "closed"]
TICKET_PRIORITIES = ["low", "medium", "high", "urgent"]
DEAL_STAGES = ["prospecting", "proposal", "negotiation", "won", "lost"]
CONTACT_STAGES = ["lead", "qualified", "customer", "unqualified"]
EMPLOYEE_TYPES = ["full_time", "part_time", "contractor", "intern"]
INVOICE_STATUSES = ["draft", "submitted", "paid", "void"]
CANDIDATE_STATUSES = ["applied", "screening", "interview", "offer", "hired", "rejected"]

DATA_CATEGORIES = ("ticketing", "crm", "hris", "accounting", "ats", "filestorage")

COMMON_MODELS: dict[str, dict[str, Any]] = {
    "ticketing": {
        "models": ["Ticket"],
        "Ticket": {
            "fields": {
                "title": {"type": "string", "required_on_create": True},
                "status": {"type": "enum", "values": TICKET_STATUSES, "required_on_create": False},
                "priority": {"type": "enum", "values": TICKET_PRIORITIES, "required_on_create": False},
                "assignee": {"type": "string", "required_on_create": False},
                "description": {"type": "string", "required_on_create": False},
                "tags": {"type": "array[string]", "required_on_create": False},
            },
            "read_only": ["id", "remote_id", "created_at", "updated_at", "remote_data"],
        },
    },
    "crm": {
        "models": ["Contact", "Deal"],
        "Contact": {
            "fields": {
                "first_name": {"type": "string", "required_on_create": True},
                "last_name": {"type": "string", "required_on_create": False},
                "email": {"type": "string", "required_on_create": False},
                "company": {"type": "string", "required_on_create": False},
                "stage": {"type": "enum", "values": CONTACT_STAGES, "required_on_create": False},
            },
            "read_only": ["id", "remote_id", "created_at", "updated_at", "remote_data"],
        },
        "Deal": {
            "fields": {
                "name": {"type": "string", "required_on_create": True},
                "stage": {"type": "enum", "values": DEAL_STAGES, "required_on_create": False},
                "amount": {"type": "number", "required_on_create": False},
                "currency": {"type": "string", "required_on_create": False},
            },
            "read_only": ["id", "remote_id", "created_at", "updated_at", "remote_data"],
        },
    },
    "hris": {
        "models": ["Employee"],
        "Employee": {
            "fields": {
                "first_name": {"type": "string", "required_on_create": True},
                "last_name": {"type": "string", "required_on_create": False},
                "email": {"type": "string", "required_on_create": False},
                "department": {"type": "string", "required_on_create": False},
                "position": {"type": "string", "required_on_create": False},
                "manager_name": {"type": "string", "required_on_create": False},
                "employment_type": {"type": "enum", "values": EMPLOYEE_TYPES, "required_on_create": False},
            },
            "read_only": ["id", "remote_id", "created_at", "updated_at", "remote_data"],
        },
    },
    "accounting": {
        "models": ["Invoice"],
        "Invoice": {
            "fields": {
                "invoice_number": {"type": "string", "required_on_create": False},
                "status": {"type": "enum", "values": INVOICE_STATUSES, "required_on_create": False},
                "vendor_name": {"type": "string", "required_on_create": True},
                "total_amount": {"type": "number", "required_on_create": True},
                "currency": {"type": "string", "required_on_create": False},
                "issue_date": {"type": "string", "required_on_create": False},
                "due_date": {"type": "string", "required_on_create": False},
                "memo": {"type": "string", "required_on_create": False},
            },
            "read_only": ["id", "remote_id", "created_at", "updated_at", "remote_data"],
        },
    },
    "ats": {
        "models": ["Candidate"],
        "Candidate": {
            "fields": {
                "first_name": {"type": "string", "required_on_create": True},
                "last_name": {"type": "string", "required_on_create": False},
                "email": {"type": "string", "required_on_create": False},
                "current_company": {"type": "string", "required_on_create": False},
                "role": {"type": "string", "required_on_create": False},
                "location": {"type": "string", "required_on_create": False},
                "application_status": {"type": "enum", "values": CANDIDATE_STATUSES,
                                       "required_on_create": False},
                "recruiter": {"type": "string", "required_on_create": False},
                "notes": {"type": "string", "required_on_create": False},
                "tags": {"type": "array[string]", "required_on_create": False},
            },
            "read_only": ["id", "remote_id", "created_at", "updated_at", "remote_data"],
        },
    },
    "filestorage": {
        "models": ["File", "Folder"],
        "File": {
            "fields": {
                "name": {"type": "string", "required_on_create": True},
                "mime_type": {"type": "string", "required_on_create": False},
                "folder": {"type": "string", "required_on_create": False},
                "tags": {"type": "array[string]", "required_on_create": False},
            },
            "read_only": ["id", "remote_id", "size_bytes", "uploaded_by",
                          "created_at", "updated_at", "remote_data"],
        },
        "Folder": {
            "fields": {
                "name": {"type": "string", "required_on_create": False},
                "parent": {"type": "string", "required_on_create": False},
            },
            "read_only": ["id", "remote_id", "created_at", "updated_at", "remote_data"],
        },
    },
}


def meta_post_payload(category: str, model: str) -> dict:
    """The /meta/post-style discovery contract: validates before write."""
    cm = COMMON_MODELS[category][model]
    return {
        "model": model,
        "category": category,
        "fields": cm["fields"],
        "read_only_fields": cm["read_only"],
        "notes": "POST/PATCH requests are validated against this schema. Enum fields only accept listed values.",
    }
