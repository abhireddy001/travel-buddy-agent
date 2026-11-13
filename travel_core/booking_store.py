"""
Booking persistence for the Travel Agent.

- `JsonBookingStore`   : local file (default for the local MCP server)
- `DynamoBookingStore` : DynamoDB single table (used by the Gateway Lambda).
  Seat reservations are atomic: the booking item and the per-flight seat
  counter are written in one transaction with a capacity condition, so two
  concurrent bookings can never oversell a flight.

Table layout (partition key `pk`, string):
    BOOKING#<ref>                 -> booking document
    SEATS#<flight_id>#<cabin>     -> {"booked": <int>}
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Protocol


class BookingStoreError(ValueError):
    """Raised for store-level conflicts (sold out, unknown/cancelled booking)."""


class BookingStore(Protocol):
    def seats_booked(self, flight_id: str, cabin: str) -> int: ...
    def create(self, booking: dict, seats_available_before_bookings: int) -> None: ...
    def get(self, ref: str) -> dict | None: ...
    def cancel(self, ref: str) -> dict: ...


class JsonBookingStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def _load(self) -> dict[str, dict]:
        if not self.path.is_file():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def _save(self, data: dict[str, dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def seats_booked(self, flight_id: str, cabin: str) -> int:
        return sum(
            len(b["passengers"]) for b in self._load().values()
            if b["flight_id"] == flight_id and b["cabin"] == cabin and b["status"] == "CONFIRMED"
        )

    def create(self, booking: dict, seats_available_before_bookings: int) -> None:
        with self._lock:
            data = self._load()
            booked = sum(
                len(b["passengers"]) for b in data.values()
                if b["flight_id"] == booking["flight_id"] and b["cabin"] == booking["cabin"]
                and b["status"] == "CONFIRMED"
            )
            left = seats_available_before_bookings - booked
            if left < len(booking["passengers"]):
                raise BookingStoreError(
                    f"Only {max(left, 0)} {booking['cabin']} seat(s) left on {booking['flight_id']}; "
                    f"cannot book {len(booking['passengers'])}."
                )
            if booking["booking_ref"] in data:
                raise BookingStoreError("Booking reference collision; please retry.")
            data[booking["booking_ref"]] = booking
            self._save(data)

    def get(self, ref: str) -> dict | None:
        return self._load().get(ref)

    def cancel(self, ref: str) -> dict:
        with self._lock:
            data = self._load()
            if ref not in data:
                raise BookingStoreError(f"No booking found with reference '{ref}'.")
            if data[ref]["status"] == "CANCELLED":
                raise BookingStoreError(f"Booking {ref} is already cancelled.")
            data[ref]["status"] = "CANCELLED"
            self._save(data)
            return data[ref]


class DynamoBookingStore:
    def __init__(self, table_name: str, region: str | None = None):
        import boto3  # local import: not needed for the JSON store

        self.table_name = table_name
        self.client = boto3.client("dynamodb", region_name=region)
        self.table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    @staticmethod
    def _seat_key(flight_id: str, cabin: str) -> str:
        return f"SEATS#{flight_id}#{cabin}"

    def seats_booked(self, flight_id: str, cabin: str) -> int:
        item = self.table.get_item(Key={"pk": self._seat_key(flight_id, cabin)}).get("Item")
        return int(item["booked"]) if item else 0

    def create(self, booking: dict, seats_available_before_bookings: int) -> None:
        n = len(booking["passengers"])
        max_booked_before = seats_available_before_bookings - n
        if max_booked_before < 0:
            raise BookingStoreError(
                f"Only {seats_available_before_bookings} {booking['cabin']} seat(s) left on "
                f"{booking['flight_id']}; cannot book {n}."
            )
        # Booking is stored as a JSON document string (avoids float->Decimal conversion).
        item = {"pk": {"S": f"BOOKING#{booking['booking_ref']}"}, "doc": {"S": json.dumps(booking)}}
        try:
            self.client.transact_write_items(TransactItems=[
                {"Put": {
                    "TableName": self.table_name,
                    "Item": item,
                    "ConditionExpression": "attribute_not_exists(pk)",
                }},
                {"Update": {
                    "TableName": self.table_name,
                    "Key": {"pk": {"S": self._seat_key(booking["flight_id"], booking["cabin"])}},
                    "UpdateExpression": "ADD booked :n",
                    "ConditionExpression": "attribute_not_exists(booked) OR booked <= :max",
                    "ExpressionAttributeValues": {":n": {"N": str(n)}, ":max": {"N": str(max_booked_before)}},
                }},
            ])
        except self.client.exceptions.TransactionCanceledException as e:
            booked = self.seats_booked(booking["flight_id"], booking["cabin"])
            left = max(0, seats_available_before_bookings - booked)
            raise BookingStoreError(
                f"Only {left} {booking['cabin']} seat(s) left on {booking['flight_id']}; cannot book {n}."
            ) from e

    def get(self, ref: str) -> dict | None:
        item = self.table.get_item(Key={"pk": f"BOOKING#{ref}"}).get("Item")
        return json.loads(item["doc"]) if item else None

    def cancel(self, ref: str) -> dict:
        booking = self.get(ref)
        if not booking:
            raise BookingStoreError(f"No booking found with reference '{ref}'.")
        if booking["status"] == "CANCELLED":
            raise BookingStoreError(f"Booking {ref} is already cancelled.")
        booking["status"] = "CANCELLED"
        n = len(booking["passengers"])
        try:
            self.client.transact_write_items(TransactItems=[
                {"Put": {
                    "TableName": self.table_name,
                    "Item": {"pk": {"S": f"BOOKING#{ref}"}, "doc": {"S": json.dumps(booking)}},
                    "ConditionExpression": "contains(doc, :confirmed)",
                    "ExpressionAttributeValues": {":confirmed": {"S": '"status": "CONFIRMED"'}},
                }},
                {"Update": {
                    "TableName": self.table_name,
                    "Key": {"pk": {"S": self._seat_key(booking["flight_id"], booking["cabin"])}},
                    "UpdateExpression": "ADD booked :neg",
                    "ExpressionAttributeValues": {":neg": {"N": str(-n)}},
                }},
            ])
        except self.client.exceptions.TransactionCanceledException as e:
            raise BookingStoreError(f"Booking {ref} is already cancelled.") from e
        return booking
