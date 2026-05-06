# Water Billing System - Django Version

This is a Django recreation of the uploaded PHP **Water Billing & Payment System**.

## What was transferred
- Landing page / home
- User registration and login
- Consumer dashboard
- Consumer records page
- Billing records page
- Add billing page
- Payments page
- Reports / analytics page
- Profile page



## Recommended architecture change
The PHP project mixes data across **three MySQL databases**:
- `water_billing_system`
- `tabuanwater`
- `water`

For Django, the cleanest setup is to **merge these into one normalized database** using:
- `auth_user` for login
- `billing_consumerprofile`
- `billing_consumer`
- `billing_billingrecord`
- `billing_payment`

This project is already built that way.

## Quick start
```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python manage.py makemigrations
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

## Database
this system runs via msql 8.4 and uses a waterbilling_v1.26 database



## Next recommended step
After testing this Django version, the next step is to import your real MySQL data into the Django tables.
