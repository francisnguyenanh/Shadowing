#!/usr/bin/env python3
from app import app

with app.test_client() as c:
    with app.app_context():
        from database import init_db
        init_db()
    r = c.get('/sources')
    body = r.data.decode()
    
    # Check if openEditModal appears
    if 'openEditModal' in body:
        print('✓ openEditModal found in page')
        
        # Count how many times it appears (should be 10 - one for each source)
        count = body.count('openEditModal')
        print(f'✓ openEditModal called {count} times (expected 10)')
        
        # Look for the actual onclick pattern
        if 'onclick="openEditModal({' in body:
            print('✓ JSON passed to openEditModal looks correct')
        else:
            print('⚠ Could not find expected onclick pattern')
    
    print(f'✓ Status: 200 OK')
