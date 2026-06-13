from django.db import models


class FuelStation(models.Model):
    """
    Fuel stop from fuel_prices.csv.

    Station selection uses route geometry projection, not stored coordinates.
    latitude, longitude, and is_approximate are retained for schema compatibility
    but are not populated or read by application logic.
    """

    name = models.CharField(max_length=255)
    address = models.TextField()
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=50)
    price = models.FloatField()

    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    is_approximate = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.name} - {self.city}"