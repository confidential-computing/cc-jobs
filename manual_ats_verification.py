#!/usr/bin/env python3
"""
Manual ATS Verification for High-Value Targets
Tests specific members to see if their career platforms have changed or become accessible
"""

import yaml
import requests
import time
from urllib.parse import urlparse
import re
import json

def test_member_accessibility(member: dict) -> dict:
    """Test if a member's career page is now accessible or has changed platforms"""
    
    result = {
        'member_name': member['name'],
        'tier': member.get('tier', 'Unknown'),
        'current_type': member.get('type', 'no_page'),
        'careers_url': member.get('careers_url', ''),
        'current_note': member.get('note', ''),
        'accessible': False,
        'status_code': None,
        'final_url': None,
        'ats_indicators': [],
        'potential_integration': None,
        'config_suggestions': {},
        'error': None,
        'content_analysis': {}
    }
    
    if not result['careers_url']:
        result['error'] = 'No careers URL available'
        return result
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }
    
    try:
        response = requests.get(result['careers_url'], headers=headers, timeout=15, allow_redirects=True)
        result['status_code'] = response.status_code
        result['final_url'] = response.url
        
        if response.status_code == 200:
            result['accessible'] = True
            html = response.text
            html_lower = html.lower()
            
            # Check for ATS platform indicators in HTML
            ats_checks = {
                'workday': [
                    'myworkdayjobs.com',
                    'workday-cdn.com',
                    'data-automation-id=\"workday',
                    'workdayworld',
                    '/wday/cxs/'
                ],
                'greenhouse': [
                    'greenhouse.io',
                    'boards.greenhouse.io',
                    'greenhouse-api',
                    'data-greenhouse',
                    'greenhouse/embed'
                ],
                'lever': [
                    'jobs.lever.co',
                    'lever-api',
                    'data-lever',
                    'lever.co'
                ],
                'bamboohr': [
                    'bamboohr.com',
                    'bamboo-hr'
                ],
                'smartrecruiters': [
                    'smartrecruiters.com',
                    'sr-api'
                ],
                'successfactors': [
                    'successfactors',
                    'sf-recruiting',
                    'brassring'
                ],
                'icims': [
                    'icims.com',
                    'icims-api'
                ]
            }
            
            for platform, indicators in ats_checks.items():
                matches = []
                for indicator in indicators:
                    if indicator in html_lower:
                        matches.append(indicator)
                
                if matches:
                    result['ats_indicators'].append({
                        'platform': platform,
                        'matches': matches,
                        'confidence': len(matches)
                    })
            
            # Analyze content for integration potential
            result['content_analysis'] = {
                'has_job_listings': bool(re.search(r'(job|position|role|career|hiring|opening)', html_lower)),
                'has_search_form': bool(re.search(r'<form[^>]*search', html_lower)),
                'has_job_links': len(re.findall(r'href=[\"\']/[^\"\']*job[^\"\']*[\"\']', html_lower)),
                'json_data': 'application/json' in html_lower or 'json' in html_lower,
                'api_endpoints': len(re.findall(r'/api/[^\s\"\'<>]*', html_lower))
            }
            
            # Suggest integration type based on analysis
            if result['ats_indicators']:
                # Sort by confidence (number of matches)
                best_match = max(result['ats_indicators'], key=lambda x: x['confidence'])
                platform = best_match['platform']
                
                if platform in ['workday', 'greenhouse', 'lever']:
                    result['potential_integration'] = platform
                    
                    # Try to extract configuration
                    if platform == 'workday' and 'myworkdayjobs.com' in response.url:
                        # Extract from final URL if redirected to Workday
                        workday_match = re.search(r'https://([^.]+)\.(wd\\d+)\\.myworkdayjobs\\.com/([^/?]*)', response.url)
                        if workday_match:
                            result['config_suggestions'] = {
                                'workday_api': f'https://{workday_match.group(1)}.{workday_match.group(2)}.myworkdayjobs.com/wday/cxs/{workday_match.group(1)}/{workday_match.group(3)}/jobs',
                                'workday_job_base': f'https://{workday_match.group(1)}.{workday_match.group(2)}.myworkdayjobs.com/{workday_match.group(3)}'
                            }
                    
                    elif platform == 'greenhouse':
                        # Look for greenhouse board slug in HTML
                        gh_matches = re.findall(r'boards\\.greenhouse\\.io/([a-zA-Z0-9\\-_]+)', html)
                        if gh_matches:
                            result['config_suggestions'] = {'slug': gh_matches[0]}
                    
                    elif platform == 'lever':
                        # Look for lever board slug in HTML
                        lever_matches = re.findall(r'jobs\\.lever\\.co/([a-zA-Z0-9\\-_]+)', html)
                        if lever_matches:
                            result['config_suggestions'] = {'slug': lever_matches[0]}
                
                else:
                    result['potential_integration'] = 'gated'  # Requires special handling
                    
            elif result['content_analysis']['has_job_listings']:
                result['potential_integration'] = 'fetch'  # Simple page scraping
            
        else:
            result['error'] = f'HTTP {response.status_code}'
            
    except requests.RequestException as e:
        result['error'] = f'Request failed: {str(e)}'
    
    return result

def test_high_value_members():
    """Test the high-value members identified for potential ATS migration"""
    
    # Load current config
    with open('members.yaml') as f:
        data = yaml.safe_load(f)
    
    # Target specific high-value members
    target_names = [
        'Intel Corporation',
        'TikTok Inc.',
        'Arm Limited', 
        'Qualcomm Technologies, Inc.',
        'Ampere Computing'
    ]
    
    target_members = []
    for member in data['members']:
        if member['name'] in target_names:
            target_members.append(member)
    
    print(f'🎯 TESTING {len(target_members)} HIGH-VALUE MEMBERS FOR ATS CHANGES')
    print('=' * 70)
    print()
    
    results = []
    
    for i, member in enumerate(target_members):
        print(f'[{i+1}/{len(target_members)}] Testing {member["name"]} ({member.get("tier", "Unknown")})...')
        
        result = test_member_accessibility(member)
        results.append(result)
        
        # Display results
        if result['accessible']:
            print(f'  ✅ ACCESSIBLE (HTTP {result["status_code"]})')
            
            if result['ats_indicators']:
                print(f'  🎯 ATS DETECTED:')
                for indicator in result['ats_indicators']:
                    platform = indicator['platform']
                    confidence = indicator['confidence']
                    print(f'     {platform.upper()} (confidence: {confidence})')
                
                if result['potential_integration']:
                    print(f'  💡 SUGGESTED: {result["potential_integration"]}')
                    if result['config_suggestions']:
                        print(f'     Config: {result["config_suggestions"]}')
            else:
                print(f'  📄 Content: {"Jobs found" if result["content_analysis"]["has_job_listings"] else "No jobs detected"}')
                if result['potential_integration']:
                    print(f'  💡 SUGGESTED: {result["potential_integration"]}')
        else:
            print(f'  ❌ NOT ACCESSIBLE: {result["error"]}')
        
        print()
        
        # Respectful delay
        time.sleep(2)
    
    return results

def generate_member_updates(results: list) -> list:
    """Generate suggested members.yaml updates"""
    
    updates = []
    
    for result in results:
        if (result['accessible'] and 
            result['potential_integration'] in ['workday', 'greenhouse', 'lever', 'fetch']):
            
            update = {
                'name': result['member_name'],
                'action': 'update_type',
                'current_type': result['current_type'],
                'new_type': result['potential_integration'],
                'careers_url': result['careers_url'],
                'ats_detected': result['ats_indicators'][0]['platform'] if result['ats_indicators'] else None,
                'config': result['config_suggestions']
            }
            updates.append(update)
    
    return updates

if __name__ == '__main__':
    print('🔍 Manual ATS Verification for High-Value Members')
    print('=================================================')
    
    results = test_high_value_members()
    
    # Generate update suggestions
    updates = generate_member_updates(results)
    
    print('📊 VERIFICATION SUMMARY')
    print('=======================')
    accessible = len([r for r in results if r['accessible']])
    with_ats = len([r for r in results if r['ats_indicators']])
    ready_for_update = len(updates)
    
    print(f'Members tested: {len(results)}')
    print(f'Now accessible: {accessible}')
    print(f'ATS platforms detected: {with_ats}')
    print(f'Ready for integration: {ready_for_update}')
    
    if updates:
        print()
        print('🎯 RECOMMENDED UPDATES')
        print('======================')
        
        for update in updates:
            print(f'\n{update["name"]}:')
            print(f'  Change: {update["current_type"]} → {update["new_type"]}')
            if update['ats_detected']:
                print(f'  Platform: {update["ats_detected"].upper()}')
            if update['config']:
                for key, value in update['config'].items():
                    print(f'  {key}: {value}')
    
    print('\n✨ Manual verification complete!')
