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

## Default database
This starter uses **SQLite** so you can run it immediately.

## If you want MySQL instead
Update `waterbilling_project/settings.py` and replace the `DATABASES` section with your MySQL credentials.

## Notes about the old project
The uploaded PHP version has some inconsistencies that were cleaned during transfer:
- some pages read from `users`, others from `consumers`
- `billing` fields vary across files (`bill_id`, `billing_id`, `amount`, `total_amount`)
- some values are hard-coded in the dashboard
- some logic duplicates records into multiple databases

## Next recommended step
After testing this Django version, the next step is to import your real MySQL data into the Django tables.
