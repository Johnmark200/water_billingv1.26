#!/usr/bin/env python
import os
import sys


def main():
    # Set the default Django settings module for the water_billingdb project
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'waterbilling_project.settings')
    
    # Ensure we are using the correct database version (water_billingdb_v2_1)
    os.environ.setdefault('DJANGO_DB_NAME', 'water_billingdb_v2_1')  # If needed for specific DB versioning
    
    # Execute Django command line utilities
    from django.core.management import execute_from_command_line
    execute_from_command_line(sys.argv)
    
    
if __name__ == '__main__':
    main()
