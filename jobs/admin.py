from django.contrib import admin

from .models import Job, OutboxEvent, PropertyRecord
from .services import redrive_dead_letter


@admin.register(Job)
class JobAdmin(admin.ModelAdmin):
    list_display = [
        "id",
        "job_type",
        "status",
        "progress",
        "attempts",
        "available_at",
        "leased_until",
        "created_at",
    ]
    list_filter = ["status", "job_type"]
    search_fields = ["id", "idempotency_key"]
    readonly_fields = ["id", "created_at", "updated_at"]
    actions = ["redrive"]

    @admin.action(description="Redrive selected dead-letter jobs")
    def redrive(self, request, queryset):
        count = redrive_dead_letter(list(queryset.values_list("pk", flat=True)))
        self.message_user(request, f"Redriven {count} dead-letter job(s).")


@admin.register(OutboxEvent)
class OutboxEventAdmin(admin.ModelAdmin):
    list_display = ["id", "job", "event_type", "status", "created_at", "dispatched_at"]
    list_filter = ["status", "event_type"]
    search_fields = ["job__id"]


@admin.register(PropertyRecord)
class PropertyRecordAdmin(admin.ModelAdmin):
    list_display = ["id", "external_id", "city", "postcode", "price", "bedrooms", "job"]
    list_filter = ["city"]
    search_fields = ["external_id", "postcode", "job__id"]
