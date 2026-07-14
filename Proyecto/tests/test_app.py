import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import app as app_module


class AppTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.app = app_module.create_app(db_path=os.path.join(self.tmpdir, "test.db"))
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def test_homepage_renders(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Sef Peluqueria", response.data)
        self.assertIn(b"Ver turnos", response.data)

    def test_agenda_requires_password(self):
        response = self.client.get("/agenda")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Contrase\xc3\xb1a de agenda", response.data)
        self.assertNotIn(b"Agenda de turnos", response.data)

    def test_agenda_search_by_date_filters_results(self):
        self.client.post(
            "/appointments",
            data={
                "client_name": "Maria",
                "client_last_name": "Lopez",
                "phone": "555666777",
                "service": "Corte",
                "appointment_date": "21/07/2026",
                "appointment_time": "16:00",
            },
            follow_redirects=True,
        )

        self.client.post(
            "/agenda",
            data={"agenda_password": "123"},
            follow_redirects=True,
        )

        response = self.client.get("/agenda?search_date=2026-07-21")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Maria Lopez", response.data)

    def test_agenda_opens_filtered_by_todays_date(self):
        today = datetime.now().strftime("%d/%m/%Y")
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%d/%m/%Y")

        self.client.post(
            "/appointments",
            data={
                "client_name": "Carlos",
                "client_last_name": "Diaz",
                "phone": "555000111",
                "service": "Corte",
                "appointment_date": today,
                "appointment_time": "18:00",
            },
            follow_redirects=True,
        )
        self.client.post(
            "/appointments",
            data={
                "client_name": "Pedro",
                "client_last_name": "Ruiz",
                "phone": "555000222",
                "service": "Corte",
                "appointment_date": tomorrow,
                "appointment_time": "18:30",
            },
            follow_redirects=True,
        )

        self.client.post(
            "/agenda",
            data={"agenda_password": "123"},
            follow_redirects=True,
        )

        response = self.client.get("/agenda")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Carlos Diaz", response.data)
        self.assertNotIn(b"Pedro Ruiz", response.data)

    def test_can_create_and_lookup_appointment(self):
        response = self.client.post(
            "/appointments",
            data={
                "client_name": "Juan",
                "client_last_name": "Perez",
                "phone": "123456789",
                "service": "Corte",
                "appointment_date": "21/07/2026",
                "appointment_time": "15:30",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Turno creado", response.data)
        self.assertIn("Tu código de turno", response.get_data(as_text=True))

        with self.app.app_context():
            appointments = app_module.get_appointments()
        self.assertTrue(appointments)
        access_code = appointments[0]["access_code"]

        lookup_response = self.client.post(
            "/appointments/lookup",
            data={"access_code": access_code},
            follow_redirects=True,
        )
        self.assertEqual(lookup_response.status_code, 200)
        self.assertIn("Juan", lookup_response.get_data(as_text=True))

    def test_rejects_out_of_schedule_datetime(self):
        response = self.client.post(
            "/appointments",
            data={
                "client_name": "Ana",
                "client_last_name": "Gomez",
                "phone": "987654321",
                "service": "Corte + Barba",
                "appointment_date": "20/07/2026",
                "appointment_time": "09:00",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Fuera del horario", response.data)

    def test_availability_endpoint_excludes_booked_slots(self):
        self.client.post(
            "/appointments",
            data={
                "client_name": "Luis",
                "client_last_name": "Lopez",
                "phone": "111222333",
                "service": "Corte",
                "appointment_date": "21/07/2026",
                "appointment_time": "15:30",
            },
            follow_redirects=True,
        )

        response = self.client.get("/availability?appointment_date=21/07/2026")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertIn("available_times", data)
        self.assertNotIn("15:30", data["available_times"])

    def test_build_google_event_body_uses_valid_datetime(self):
        appointment = {
            "client_name": "Juan",
            "client_last_name": "Perez",
            "phone": "123456789",
            "service": "Corte",
            "appointment_date": "21/07/2026",
            "appointment_time": "15:30",
        }
        with self.app.app_context():
            event_body = app_module.build_google_event_body(appointment)

        self.assertEqual(event_body["summary"], "Corte - Juan")
        self.assertEqual(event_body["start"]["dateTime"], "2026-07-21T15:30:00-03:00")
        self.assertEqual(event_body["end"]["dateTime"], "2026-07-21T16:00:00-03:00")

    def test_can_update_and_delete_appointment(self):
        create_response = self.client.post(
            "/appointments",
            data={
                "client_name": "Ana",
                "client_last_name": "Gomez",
                "phone": "987654321",
                "service": "Corte + Barba",
                "appointment_date": "21/07/2026",
                "appointment_time": "17:00",
            },
            follow_redirects=True,
        )
        self.assertEqual(create_response.status_code, 200)

        with self.app.app_context():
            appointments = app_module.get_appointments()
        self.assertTrue(appointments)
        appointment_id = appointments[0]["id"]
        access_code = appointments[0]["access_code"]

        response = self.client.post(
            f"/appointments/{appointment_id}/edit",
            data={
                "client_name": "Ana Actualizada",
                "client_last_name": "Gomez",
                "phone": "111222333",
                "service": "Corte",
                "appointment_date": "22/07/2026",
                "appointment_time": "18:00",
                "access_code": access_code,
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Ana Actualizada", response.data)

        delete_response = self.client.post(
            f"/appointments/{appointment_id}/delete",
            data={"access_code": access_code},
            follow_redirects=True,
        )
        self.assertEqual(delete_response.status_code, 200)
        self.assertIn(b"Turno eliminado", delete_response.data)


if __name__ == "__main__":
    unittest.main()
