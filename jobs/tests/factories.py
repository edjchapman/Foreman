import factory

from jobs.models import Job


class JobFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Job

    job_type = "property_csv_import"
    # Resolves to the bundled fixture so factory-built jobs ingest cleanly.
    payload = factory.LazyFunction(lambda: {"source": "sample:properties.csv"})
