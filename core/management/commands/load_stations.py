import pandas as pd

from django.core.management.base import BaseCommand

from core.models import FuelStation

CSV_PATH = "data/fuel_prices.csv"
BATCH_SIZE = 500


class Command(BaseCommand):
    help = "Load fuel stations from CSV (no geocoding; lat/lng left null)"

    def handle(self, *args, **kwargs):
        df = pd.read_csv(CSV_PATH)
        self.stdout.write(f"Total rows in CSV: {len(df)}")

        existing_keys = set(
            FuelStation.objects.values_list("name", "city", "state")
        )
        self.stdout.write(f"Already in database: {len(existing_keys)}")

        stations_to_create = []
        skipped = 0
        created = 0

        for _, row in df.iterrows():
            name = row["Truckstop Name"]
            city = row["City"]
            state = row["State"]

            if (name, city, state) in existing_keys:
                skipped += 1
                continue

            stations_to_create.append(
                FuelStation(
                    name=name,
                    address=row["Address"],
                    city=city,
                    state=state,
                    price=row["Retail Price"],
                )
            )
            existing_keys.add((name, city, state))
            created += 1

            if len(stations_to_create) >= BATCH_SIZE:
                FuelStation.objects.bulk_create(stations_to_create)
                self.stdout.write(f"Inserted batch of {BATCH_SIZE}")
                stations_to_create = []

        if stations_to_create:
            FuelStation.objects.bulk_create(stations_to_create)
            self.stdout.write(f"Inserted final batch of {len(stations_to_create)}")

        self.stdout.write(f"Created: {created}, skipped (already present): {skipped}")
        self.stdout.write("Done.")
