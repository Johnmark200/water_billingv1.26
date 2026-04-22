# Legacy project observations

## Stack found in uploaded ZIP
- Frontend: HTML + CSS + vanilla JavaScript
- Backend: PHP (PDO)
- Database: MySQL
- Structure: separate admin/consumer/config pages

## Main legacy features found
- Login and signup
- Consumer list
- Billing record creation
- Payment listing
- Reports dashboard
- Profile update and password change

## Important legacy issue
The old app connects to three databases and sometimes copies the same user or billing data into all three. This makes reporting harder and increases duplication risk.

## Recommended Django migration strategy
1. Consolidate the data model.
2. Rebuild authentication with Django auth.
3. Move billing and payment logic into Django models.
4. Reuse the old CSS and images as static assets.
5. Import historical data after the new schema is stable.
