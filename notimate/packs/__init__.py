"""Vertical packs: industry-specific business logic layered on top of NotiMate Core.

A tenant's `vertical_pack` column (notimate/tenant_store.py) selects which pack, if any,
notimate.pipeline.process_whatsapp_event dispatches an inbound message to instead of the
plain acknowledgement reply.
"""
