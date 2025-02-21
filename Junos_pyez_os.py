import getpass
import re
import time
import subprocess
import difflib
import tabulate
 
from jnpr.junos import Device
from jnpr.junos.utils.scp import SCP
from jnpr.junos.utils.fs import FS
from jnpr.junos.utils.sw import SW
from jnpr.junos.exception import ConnectError, RpcError, CommitError
 
def establish_ssh_connection(hostname, username, password):
    """Establish a connection to the Juniper device using PyEZ."""
    try:
        print(f"Connecting to {hostname}...")
        dev = Device(host=hostname, user=username, passwd=password)
        dev.open()
        print(f"Connected to {hostname}.")
        return dev
    except ConnectError as e:
        print(f"Failed to connect to {hostname}: {e}")
        return None
   
def validate_junos_os(dev,extracted_os):
    try:
        facts = dev.facts
        current_os = facts.get("version")  
        if current_os in extracted_os:
            print(f"Junos is already updated  {current_os}")
            return True
        return False
    except Exception as e:
        print(f"Error fetching current os: {e}")
        return False
   
def parse_interfaces_descriptions(output, regex_pattern):
    """Parse 'show interfaces descriptions' to find all links and their statuses."""
    interfaces = {}
    lines = output.splitlines()
   
    current_interface = None
    for line in lines[1:]:
        if line.startswith("name:"):
            current_interface = line.split(": ")[-1].strip()
            interfaces[current_interface] = {}
        # elif "admin status:" in line:
        #     if  line.split(": ")[-1].strip().lower() == "up":
        #         interfaces[current_interface]["admin_up"] = line.split(": ")[-1].strip().lower() == "up"
        #     else:
        #         interfaces[current_interface]["admin_up"] = False
        # elif "oper status:" in line:
        #     if line.split(": ")[-1].strip().lower() == "up":
        #         interfaces[current_interface]["link_up"] = line.split(": ")[-1].strip().lower() == "up"
        #     else:
        #         interfaces[current_interface]["link_up"] = False
        elif "admin status:" in line or "oper status:" in line:
            status = line.split(": ")[-1].strip().lower()
            key = "admin_up" if "admin status:" in line else "link_up"
            interfaces[current_interface][key] = status == "up"
        elif "description:" in line:
            description = line.split(": ")[-1].strip()
            interfaces[current_interface]["description"] = description
            interfaces[current_interface]["is_uplink"] = re.search(regex_pattern, description) is not None
   
    return interfaces
 
def checks(dev, check_commands):
    check_output = ""
    for cmd in check_commands:
        try:
            print(cmd['description'])
            rpc_method = getattr(dev.rpc, cmd['method'])
            output = rpc_method(**cmd.get('args', {}))
 
            if output is None:
                text_output = "No data available"
            else:
                text_output = extract_text_from_xml(output)
 
            check_output += f"Command: {cmd['description']}\n{text_output}\n{'-'*50}\n"
        except Exception as e:
            check_output += f"Error executing {cmd['description']}: {e}\n{'-'*50}\n"
    return check_output
 
def save_output_to_file(filename, output):
    """Save command output to a file."""
    with open(filename, 'w') as file:
        file.write(output)
 
def copy_firmware(dev, firmware_path, destination):
    """Copy firmware to the device using PyEZ FileSystem."""
    try:
        fs = FS(dev)
        print(f"Checking if {firmware_path} exists on the target device...")
        file_info = fs.ls(f"/var/tmp/{firmware_path}")
 
        if not file_info:  # If file does not exist
            print(f"{firmware_path} not found on the target device. Copying it from the corpjump server...")
 
            try:
                with SCP(dev, progress=True) as scp:
                    scp.put(firmware_path, destination)
                print(f"Firmware {firmware_path} copied to {destination}.")
            except Exception as scp_error:
                print(f"SCP copy failed: {scp_error}")
                return False
        else:
            print(f"{firmware_path} already exists on the target device.")
       
        return True  # Indicate success
    except Exception as e:
        print(f"Failed to copy firmware: {e}")
        return False
 
def validate_firmware(dev, firmware, expected_md5):
    """Validate the firmware MD5 checksum."""
    try:
        fs = FS(dev)
            # Get MD5 checksum
        output = fs.checksum(firmware, calc='md5')    
        match = re.search(r'[a-fA-F0-9]{32}', output)
        if match:
            device_md5 = match.group(0)
            if device_md5 != expected_md5:
                print(f"MD5 mismatch! Expected: {expected_md5}, Found: {device_md5}")
                return False
            print("MD5 checksum verified.")
            return True
        print("Could not retrieve MD5 checksum.")
        return False
    except Exception as e:
        print("Failed to validate firmware MD5 checksum.",e)
        return False
 
def upgrade_firmware(dev, firmware_path):
    try:
        # Initialize the software utility
        sw = SW(dev)
 
        print("Starting firmware upgrade...")
        # Perform os upgrade
        result = sw.install(package=firmware_path,
                                                no_copy=True,
                                                            validate=False,          # Since the package is already on the device
                                                             )
        # Check installation result
        if result:
            print("Firmware install was successful.\nThe system is going to Reboot")      
            dev.rpc.request_reboot()  
        else:
            print("Software upgrade failed.")
            return False
 
    except RpcError as re:
        print(f"RPC Error: {re}")
        return False
    except CommitError as cm:
        print(f"Commit Error: {cm}")
        return False
    except Exception as e:
        print(f"An error occurred: {e}")
        return False
 
    return True  # Indicate success
 
def is_device_reachable(hostname):
    """Ping the device to check if it's reachable."""
    response = subprocess.run(['ping', '-c', '1', '-W', '3', hostname], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return response.returncode == 0
 
def extract_text_from_xml(xml_element):
    """Extracts relevant information from an XML element and formats it as readable text."""
    output = []
    for element in xml_element.iter():
        if element.tag is not None and element.text is not None:
            tag = element.tag.replace("-", " ").strip()  # Replace hyphens for readability
            text = element.text.strip()
            if text:
                output.append(f"{tag}: {text}")
 
    return "\n".join(output)
 
def compare_files(pre_data, post_data,check_commands):
    """Compare pre-check and post-check data."""
 
    # Get differences using difflib
    delta = list(difflib.Differ().compare(pre_data.splitlines(), post_data.splitlines()))
 
    # Prepare old and new changes with alignment
    old_changes, new_changes = [], []
    for line in delta:
        if any(cmd['description'] in line for cmd in check_commands): # for printing show commands in the table
            old_changes.extend(['',line,''])
            #old_changes.append('')  
            #old_changes.append(line)
            #old_changes.append('')
            new_changes.extend(['','',line])
            # new_changes.append('')
            # new_changes.append('')
            # new_changes.append(line)
 
        elif line.startswith("- "): # for printing the changes in pre_comands
            old_changes.append(line[1:])
            new_changes.append("")
 
        elif line.startswith("+ "):       # for printing the changes in post_commands
            new_changes.append(line[1:])
            old_changes.append("")
    # Print the table
    changes = tabulate(list(zip(old_changes,new_changes[1:])), headers=["Pre_check (-)", "Post_check (+)"], tablefmt="grid")
 
    return changes
 
def main():
    username = input("Enter your username: ")
    password = getpass.getpass("Enter your password: ")
    target_device = input("Enter switch hostname: ")
    firmware_list = input("Enter firmware list (comma-separated): ").split(',')
    firmware_md5 = {}
    for firmware in firmware_list:
        firmware = firmware.strip()
        md5_value = input(f"Enter the expected MD5 checksum for {firmware}: ")
        firmware_md5[firmware] = md5_value
    os_version_regex=r'(\d+\.\d+R\d+-S\d+\.\d+)'    # dyamic according to the switch model
    uplink_regex = r"corp-cr"
    check_commands = [
        {'description': 'Show version', 'method': 'get_software_information'},
        {'description': 'Show interfaces terse', 'method': 'get_interface_information', 'args': {'terse': True}},
        {'description': 'Show LLDP neighbors', 'method': 'get_lldp_neighbors_information'},
        {'description': 'Show virtual-chassis', 'method': 'get_virtual_chassis_information'},
        {'description': 'Show interfaces description', 'method': 'get_interface_information', 'args': {'descriptions': True}},
        {'description': 'Show Ethernet Switching Table', 'method': 'get_ethernet_switching_table_information'}
    ]
 
    dev = establish_ssh_connection(target_device, username, password)
    if not dev:
        return
 
    #version check if same
    os_match = re.search(os_version_regex, list(firmware_md5.keys())[0])
    if not os_match:
        print("Error: Unable to extract Junos OS version from the firmware filename.")
        dev.close()
        return
 
    if validate_junos_os(dev, os_match.group(1)):  # Validate OS version
        print("Existing OS. Exiting from the host.")
        dev.close()
        return
 
    # Step 1: Fetch interface descriptions
    interface_output = checks(dev,[check_commands[4]])
    interfaces_status = parse_interfaces_descriptions(interface_output, uplink_regex)
    uplink_candidates = [intf for intf, details in interfaces_status.items() if details["is_uplink"]]
    active_uplinks = [intf for intf in uplink_candidates if (interfaces_status[intf]["admin_up"] and interfaces_status[intf]["link_up"])]
 
    print(f"Total uplinks: {len(uplink_candidates)}")
    print(f"Active uplinks: {len(active_uplinks)}")
 
    if len(interfaces_status) == 1 or (len(interfaces_status) == 2 and len(active_uplinks) == 1):
        if input("\nAre the uplink statuses satisfactory? Type 'Yes' to proceed the upgrade: ").strip().lower() != "yes":
            print("Exiting. Resolve issues before retrying.")
            dev.close()
            return
       
    # Step 2: Pre-check commands
    print(" Pre-checks completed for below commands:")
    pre_check_output = checks(dev,check_commands)
    save_output_to_file("pre_check.txt", pre_check_output)
 
    for firmware_path, expected_md5 in firmware_md5.items():
        # Step 3: Copy and validate firmware
        copy_firmware(dev, firmware_path, "/var/tmp/")
        dev.close()
        dev.open()
        if not validate_firmware(dev, f"/var/tmp/{firmware_path}", expected_md5):
            print("Firmware validation failed. Exiting.")
            dev.close()
            return
 
        # Step 4: Upgrade firmware
        upgrade_firmware(dev, f"/var/tmp/{firmware_path}")
 
        # Step 5: Wait for device to reboot
        print("Waiting for device to come online...")
        time.sleep(300)
        while not is_device_reachable(target_device):
            print("Device is not reachable. Upgrade in Progress...")
            time.sleep(30)
 
        print("Device is back online. Reconnecting...")
        dev = establish_ssh_connection(target_device, username, password)
        if not dev:
            print("Reconnection failed. Exiting.")
            return
 
    # Step 6: Post-checks
    print(" Post-checks completed for below commands:")
    post_check_output = checks(dev,check_commands)
    save_output_to_file("post_check.txt", post_check_output)
 
 
    # Step 7: Compare pre-check and post-check
    changes = compare_files(pre_check_output,post_check_output,check_commands)
    save_output_to_file(changes, "version_comparison.txt")
 
    print("Upgrade process completed successfully.")
 
if __name__ == "__main__":
    main()
