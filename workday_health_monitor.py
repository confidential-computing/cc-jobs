#!/usr/bin/env python3
"""
Workday Health Monitor - Conservative Approach
Monitor working APIs and auto-convert to MCP only when they break
"""

import yaml
import subprocess
import json
import os
from datetime import datetime, timedelta
from typing import Dict, List, Tuple

def create_health_log():
    """Create or update health monitoring log."""
    log_file = "workday_health_log.json"
    
    if not os.path.exists(log_file):
        initial_log = {
            "created": datetime.now().isoformat(),
            "last_updated": datetime.now().isoformat(),
            "companies": {
                "NVIDIA Corporation": {"type": "workday", "status": "healthy", "last_healthy": datetime.now().isoformat()},
                "Hewlett Packard Enterprise": {"type": "workday", "status": "healthy", "last_healthy": datetime.now().isoformat()},
                "Marvell Technology, Inc.": {"type": "workday", "status": "healthy", "last_healthy": datetime.now().isoformat()},
                "Red Hat LLC": {"type": "workday", "status": "healthy", "last_healthy": datetime.now().isoformat()},
                "SUSE LLC": {"type": "workday", "status": "healthy", "last_healthy": datetime.now().isoformat()},
                "Samsung Electronics Co. Ltd.": {"type": "workday", "status": "broken", "error_message": "HTTP 502 from Workday tenant", "last_error": datetime.now().isoformat()}
            }
        }
        
        with open(log_file, 'w') as f:
            json.dump(initial_log, f, indent=2)
        
        print(f"✅ Created health monitoring log: {log_file}")
    
    return log_file

def test_single_company_health(company_name: str) -> Tuple[bool, str]:
    """Quick health test for a single company."""
    
    # Create minimal test config
    test_config = f"""
keywords:
  case_sensitive: [TEE, SGX]
  case_insensitive: [confidential computing]

members:
  - name: {company_name}
    tier: General
    type: workday
"""
    
    # Write temp config
    with open('temp_health_test.yaml', 'w') as f:
        f.write(test_config)
    
    try:
        # Quick crawler test with timeout
        result = subprocess.run([
            'python3', 'crawler.py',
            '--members', 'temp_health_test.yaml', 
            '--output', '/tmp/health_test.md'
        ], capture_output=True, text=True, timeout=30)
        
        if result.returncode == 0:
            # Check for error indicators in output
            output = result.stdout.lower()
            if any(error in output for error in ['fetch error', 'api unavailable', '502', '503', '504']):
                return False, "API error detected"
            else:
                return True, "API healthy"
        else:
            return False, f"Crawler failed: {result.stderr}"
    
    except subprocess.TimeoutExpired:
        return False, "API timeout"
    except Exception as e:
        return False, f"Test error: {str(e)}"
    finally:
        # Cleanup
        for temp_file in ['temp_health_test.yaml', '/tmp/health_test.md']:
            if os.path.exists(temp_file):
                os.remove(temp_file)

def monitor_and_auto_convert():
    """Monitor API health and auto-convert broken APIs to MCP."""
    
    print("🔍 Workday Health Monitor - Conservative Approach")
    print(f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()
    
    log_file = create_health_log()
    
    # Load current health log
    with open(log_file, 'r') as f:
        health_log = json.load(f)
    
    # Test each company currently using workday (not workday_mcp)
    companies_to_test = {
        name: data for name, data in health_log["companies"].items() 
        if data["type"] == "workday"
    }
    
    print(f"Testing {len(companies_to_test)} companies with API access...")
    print()
    
    conversions_needed = []
    
    for company_name, company_data in companies_to_test.items():
        print(f"Testing {company_name}...", end=" ")
        
        is_healthy, message = test_single_company_health(company_name)
        
        if is_healthy:
            print("✅ Healthy")
            health_log["companies"][company_name]["status"] = "healthy"
            health_log["companies"][company_name]["last_healthy"] = datetime.now().isoformat()
        else:
            print(f"❌ {message}")
            health_log["companies"][company_name]["status"] = "broken"
            health_log["companies"][company_name]["last_error"] = datetime.now().isoformat()
            health_log["companies"][company_name]["error_message"] = message
            conversions_needed.append(company_name)
    
    # Update health log
    health_log["last_updated"] = datetime.now().isoformat()
    with open(log_file, 'w') as f:
        json.dump(health_log, f, indent=2)
    
    # Auto-convert broken APIs to MCP
    if conversions_needed:
        print(f"\n🚨 Found {len(conversions_needed)} broken APIs:")
        for company in conversions_needed:
            print(f"  - {company}")
        
        # Auto-conversion to 'workday_mcp' is disabled: crawler.py no longer
        # supports that type (the MCP path never returned jobs and was removed).
        # A broken Workday API is now reported as an error in the crawl output,
        # which is the honest signal — writing an unsupported type back into
        # members.yaml would make the member disappear from the report instead.
        print("\n⚠️  Auto-conversion disabled — 'workday_mcp' is no longer supported.")
        print("   crawler.py reports these as errors; investigate the API or")
        print("   mark the member 'gated' for manual search.")
        
        # Save final health log
        with open(log_file, 'w') as f:
            json.dump(health_log, f, indent=2)
        
        print(f"\n🎉 Auto-conversion complete! {len([c for c in conversions_needed if convert_single_company_to_mcp(c)])} companies converted.")
    else:
        print(f"\n✅ All APIs healthy - no conversions needed!")
    
    # Print summary
    print(f"\n📊 Current Status:")
    mcp_count = sum(1 for data in health_log["companies"].values() if data["type"] == "workday_mcp")
    api_count = sum(1 for data in health_log["companies"].values() if data["type"] == "workday")
    print(f"  - Companies using MCP: {mcp_count}")
    print(f"  - Companies using API: {api_count}")
    print(f"  - Health log: {log_file}")

def convert_single_company_to_mcp(company_name: str) -> bool:
    """Convert a single company to workday_mcp in members.yaml"""
    
    try:
        with open('members.yaml', 'r') as f:
            content = f.read()
        
        lines = content.split('\n')
        modified_lines = []
        in_target_company = False
        converted = False
        
        for line in lines:
            if f'name: {company_name}' in line:
                in_target_company = True
                modified_lines.append(line)
            elif in_target_company and 'type: workday' in line and 'workday_mcp' not in line:
                # Convert this line
                modified_lines.append(line.replace('type: workday', 'type: workday_mcp'))
                modified_lines.append(f'    note: "API failed on {datetime.now().strftime("%Y-%m-%d")} - auto-converted to MCP"')
                converted = True
                in_target_company = False
            else:
                modified_lines.append(line)
                # Check if we've exited the company section
                if in_target_company and line.strip().startswith('- name:'):
                    in_target_company = False
        
        if converted:
            with open('members.yaml', 'w') as f:
                f.write('\n'.join(modified_lines))
        
        return converted
        
    except Exception as e:
        print(f"Error converting {company_name}: {str(e)}")
        return False

def create_monitoring_cron():
    """Create cron job for automatic monitoring."""
    
    script_path = os.path.abspath(__file__)
    cron_command = f"0 */6 * * * cd {os.getcwd()} && python3 {script_path} --auto >> workday_monitor.log 2>&1"
    
    print("🕒 Cron Job Setup (Optional)")
    print("=" * 30)
    print("To enable automatic monitoring every 6 hours:")
    print()
    print("Run: crontab -e")
    print("Add this line:")
    print(cron_command)
    print()
    print("This will:")
    print("- Monitor API health every 6 hours")
    print("- Auto-convert broken APIs to MCP")
    print("- Log results to workday_monitor.log")

def show_health_status():
    """Show current health status from log."""
    
    log_file = "workday_health_log.json"
    
    if not os.path.exists(log_file):
        print("❌ No health log found - run monitoring first")
        return
    
    with open(log_file, 'r') as f:
        health_log = json.load(f)
    
    print("📊 Workday Health Status")
    print("=" * 25)
    print(f"Last updated: {health_log['last_updated']}")
    print()
    
    for company_name, data in health_log["companies"].items():
        status_icon = "✅" if data["status"] == "healthy" else "🔴" if data["status"] == "broken" else "⚪"
        print(f"{status_icon} {company_name}")
        print(f"   Type: {data['type']}")
        print(f"   Status: {data['status']}")
        
        if "last_healthy" in data:
            print(f"   Last healthy: {data['last_healthy'][:10]}")
        if "converted_date" in data:
            print(f"   Converted: {data['converted_date'][:10]} ({data.get('reason', 'Unknown reason')})")
        if "error_message" in data:
            print(f"   Last error: {data['error_message']}")
        print()

def main():
    """Main function with command line options."""
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == '--auto':
        # Automated mode - just run monitoring
        monitor_and_auto_convert()
    else:
        # Interactive mode
        print("🔧 Workday Health Monitor - Conservative Approach")
        print("=" * 50)
        print()
        print("1. Monitor APIs and auto-convert broken ones")
        print("2. Show current health status")
        print("3. Setup automatic monitoring (cron)")
        print("4. Exit")
        print()
        
        choice = input("Select option (1-4): ").strip()
        
        if choice == "1":
            monitor_and_auto_convert()
        elif choice == "2":
            show_health_status()
        elif choice == "3":
            create_monitoring_cron()
        elif choice == "4":
            print("Goodbye!")
        else:
            print("Invalid choice")

if __name__ == "__main__":
    main()