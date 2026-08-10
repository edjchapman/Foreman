from rest_framework import serializers

from .models import Job


class JobSerializer(serializers.ModelSerializer):
    """Read view of a job — everything is server-owned."""

    class Meta:
        model = Job
        fields = [
            "id",
            "job_type",
            "status",
            "payload",
            "progress",
            "attempts",
            "result",
            "error",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class JobCreateSerializer(serializers.ModelSerializer):
    """Submission view — the client only supplies what the job is and its inputs."""

    class Meta:
        model = Job
        fields = ["job_type", "payload"]
        # DRF omits an absent model-defaulted field from validated_data, but the
        # view indexes validated_data["job_type"] directly — supply the model's
        # default here so the key is always present.
        extra_kwargs = {"job_type": {"default": Job._meta.get_field("job_type").default}}

    def validate_payload(self, value: object) -> dict:
        if not isinstance(value, dict) or not value:
            raise serializers.ValidationError("payload must be a non-empty object.")
        return value
