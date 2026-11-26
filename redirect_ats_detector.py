#!/usr/bin/env python3
"""
Redirect-based ATS Detection for CC-jobs
Analyzes career page redirects to find hidden ATS platforms
"""

import yaml
import requests
import time
from urllib.parse import urlparse
import re

def follow_redirects_for_ats(url: str, max_redirects: int = 5) -> dict:
    """Follow redirects to detect ATS platforms"""
    
    result = {
        'original_url': url,
        'final_url': None,
        'redirect_chain': [],
        'ats_detected': None,
        'integration_type': None,
        'config': {},
        'error': None
    }
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }
    
    try:
        # Follow redirects manually to track the chain
        current_url = url
        session = requests.Session()
        
        for i in range(max_redirects):
            response = session.get(current_url, headers=headers, allow_redirects=False, timeout=10)
            
            result['redirect_chain'].append({
                'url': current_url,
                'status': response.status_code,
                'location': response.headers.get('Location', '')
            })
            
            # Check if this URL indicates an ATS platform
            domain = urlparse(current_url).netloc.lower()
            
            if 'myworkdayjobs.com' in domain:
                result['ats_detected'] = 'workday'
                result['integration_type'] = 'workday'
                
                # Extract workday configuration
                workday_match = re.search(r'https://([^.]+)\.(wd\d+)\.myworkdayjobs\.com/([^/?]*)', current_url)
                if workday_match:
                    company_slug = workday_match.group(1)
                    wd_instance = workday_match.group(2)
                    job_board = workday_match.group(3) or 'jobs'
                    
                    result['config'] = {
                        'workday_api': f'https://{company_slug}.{wd_instance}.myworkdayjobs.com/wday/cxs/{company_slug}/{job_board}/jobs',
                        'workday_job_base': f'https://{company_slug}.{wd_instance}.myworkdayjobs.com/{job_board}'
                    }
                break
                
            elif 'greenhouse.io' in domain or 'boards.greenhouse.io' in domain:
                result['ats_detected'] = 'greenhouse'
                result['integration_type'] = 'greenhouse'
                
                # Extract greenhouse slug
                gh_match = re.search(r'boards\.greenhouse\.io/([^/?]+)', current_url)
                if gh_match:
                    result['config'] = {'slug': gh_match.group(1)}
                break
                
            elif 'jobs.lever.co' in domain:
                result['ats_detected'] = 'lever'
                result['integration_type'] = 'lever'
                
                # Extract lever slug
                lever_match = re.search(r'jobs\.lever\.co/([^/?]+)', current_url)
                if lever_match:
                    result['config'] = {'slug': lever_match.group(1)}
                break
            
            # Check for redirects
            if response.status_code in [301, 302, 303, 307, 308]:
                location = response.headers.get('Location', '')
                if location:
                    if location.startswith('/'):
                        # Relative redirect
                        parsed = urlparse(current_url)
                        current_url = f'{parsed.scheme}://{parsed.netloc}{location}'
                    else:
                        current_url = location
                else:
                    break
            elif response.status_code == 200:
                # Final destination
                break
            else:
                result['error'] = f'HTTP {response.status_code}'
                break
        
        result['final_url'] = current_url
        
        # If no ATS detected from redirects, check final page content
        if not result['ats_detected'] and current_url:
            try:
                final_response = session.get(current_url, headers=headers, timeout=10)
                if final_response.status_code == 200:
                    html = final_response.text.lower()
                    
                    # Look for ATS indicators in HTML
                    if ('myworkdayjobs.com' in html or 
                        'workday-cdn.com' in html or 
                        'data-automation-id="workday' in html):
                        result['ats_detected'] = 'workday'
                        result['integration_type'] = 'gated'  # Needs JS handling
                        
                    elif ('greenhouse.io' in html or 
                          'greenhouse-api' in html or
                          'data-greenhouse' in html):
                        result['ats_detected'] = 'greenhouse'
                        result['integration_type'] = 'gated'  # May need JS
                        
                    elif ('lever.co' in html or
                          'lever-api' in html or
                          'data-lever' in html):
                        result['ats_detected'] = 'lever'
                        result['integration_type'] = 'gated'  # May need JS
                        
            except Exception as e:
                result['error'] = f'Content analysis error: {str(e)}'
        
    except requests.RequestException as e:
        result['error'] = f'Request error: {str(e)}'
    
    return result

def analyze_members_for_ats_redirects() -> list:
    """Analyze gated members for ATS platform redirects"""
    
    with open('members.yaml') as f:
        data = yaml.safe_load(f)
    
    # Target gated members with career URLs
    target_members = []
    for member in data['members']:
        if (member.get('type') == 'gated' and 
            member.get('careers_url') and 
            member.get('tier') in ['Premier', 'General']):
            target_members.append(member)
    
    results = []
    
    print(f'🔍 Analyzing {len(target_members)} gated members for ATS redirects...')
    print()
    
    for i, member in enumerate(target_members):
        name = member['name']
        tier = member['tier']
        url = member['careers_url']
        
        print(f'[{i+1}/{len(target_members)}] {name} ({tier})')
        print(f'  Testing: {url}')
        
        result = follow_redirects_for_ats(url)
        
        analysis = {
            'member': member,
            'redirect_analysis': result
        }
        results.append(analysis)
        
        if result['ats_detected']:
            print(f'  ✅ {result["ats_detected"].upper()} detected -> {result["integration_type"]}')
            if result['config']:
                print(f'     Config: {result["config"]}')
        else:
            print(f'  ❌ No ATS platform detected')
            
        if result['error']:
            print(f'  ⚠️  {result["error"]}')
            
        print(f'  Redirects: {len(result["redirect_chain"])}')
        print()
        
        # Respectful delay
        time.sleep(1.5)
    
    return results

def generate_yaml_updates(results: list) -> list:
    """Generate member.yaml updates for discovered ATS platforms"""
    
    updates = []
    
    for analysis in results:
        member = analysis['member']
        redirect_result = analysis['redirect_analysis']
        
        if (redirect_result['ats_detected'] and 
            redirect_result['integration_type'] in ['workday', 'greenhouse', 'lever']):
            
            update = {
                'name': member['name'],
                'tier': member['tier'],
                'current_type': 'gated',
                'new_type': redirect_result['integration_type'],
                'ats_platform': redirect_result['ats_detected'],
                'careers_url': member['careers_url'],
                'config': redirect_result['config'],
                'final_url': redirect_result['final_url']
            }
            updates.append(update)
    
    return updates

if __name__ == '__main__':
    print('🎯 ATS Platform Detection via Redirects')
    print('========================================')
    
    results = analyze_members_for_ats_redirects()
    
    updates = generate_yaml_updates(results)
    
    print('📊 ANALYSIS SUMMARY')
    print('===================')
    print(f'Members analyzed: {len(results)}')
    print(f'ATS platforms detected: {len([r for r in results if r["redirect_analysis"]["ats_detected"]])}')
    print(f'Ready for integration: {len(updates)}')
    
    if updates:
        print('\n🎯 RECOMMENDED UPDATES')
        print('======================')
        
        for update in updates:
            print(f'\n{update["name"]} ({update["tier"]})')
            print(f'  Change: {update["current_type"]} -> {update["new_type"]}')
            print(f'  Platform: {update["ats_platform"].upper()}')
            print(f'  URL: {update["careers_url"]}')
            if update['config']:
                for key, value in update['config'].items():
                    print(f'  {key}: {value}')
    
    print('\n✨ Redirect analysis complete!')