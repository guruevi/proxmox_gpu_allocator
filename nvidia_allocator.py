#!/usr/bin/env python3
import curses
import os
import re
import sys
from _curses import A_STANDOUT, A_UNDERLINE


def get_available_gpu(vgpu_type):
    # In /sys/bus/pci/devices/ directory find the next NVIDIA vGPU device
    # and return the path to it
    gpus, _ = get_gpus()

    # Find the first GPU that contains the vGPU type
    for gpu, vgpu_types in gpus.items():
        if vgpu_type in vgpu_types:
            return gpu

    print("No available NVIDIA vGPU found, are virtual functions enabled? (systemctl start nvidia-sriov)")
    exit(404)

VMS = []
def get_vms() -> list[tuple[str, str, dict]]:
    if VMS:
        return VMS

    for node in os.listdir('/etc/pve/nodes'):
        node_path = f'/etc/pve/nodes/{node}/qemu-server'
        if not os.path.isdir(node_path):
            continue

        for vm_file in os.listdir(node_path):
            if vm_file.endswith('.conf'):
                vmid = vm_file.split('.')[0]
                config = parse_vm_config(vmid, node)
                VMS.append((vmid, node, config))

    VMS.sort()
    return VMS

AVAILABLE_GPUS = {}
ASSIGNED_GPUS = {}
GPU_TYPES = set()

# This function returns a dictionary of available GPUs, a dictionary of assigned GPUs, and a set of available vGPU types
def get_gpus() -> (dict[str, list], dict[str, str], set):
    # In /sys/bus/pci/devices/ directory find the next NVIDIA vGPU device
    # and return the path to it
    if GPU_TYPES:
        return AVAILABLE_GPUS, ASSIGNED_GPUS, GPU_TYPES

    for device in os.listdir('/sys/bus/pci/devices/'):
        # Check if the device contains an nvidia directory
        if not os.path.isdir(f'/sys/bus/pci/devices/{device}/nvidia'):
            continue
        # Check the current_vgpu_type file to see if it is in use
        with open(f'/sys/bus/pci/devices/{device}/nvidia/current_vgpu_type') as file:
            current_vgpu_type = file.read()
        # If it is in use, continue to the next device
        if current_vgpu_type != '0\n':
            ASSIGNED_GPUS[device] = current_vgpu_type
            GPU_TYPES.add(current_vgpu_type)
            continue
        AVAILABLE_GPUS[device] = []

        with open(f'/sys/bus/pci/devices/{device}/nvidia/creatable_vgpu_types') as file:
            available_vgpu_types = file.read()

        for gpu_type in available_vgpu_types.splitlines():
            if not gpu_type or gpu_type.startswith('ID'):
                continue
            GPU_TYPES.add(gpu_type)
            vgpu_id = gpu_type.split(" : ")[0].strip()
            AVAILABLE_GPUS[device].append(vgpu_id)

    return AVAILABLE_GPUS, ASSIGNED_GPUS, GPU_TYPES

def parse_vgpu_type_id(config):
    pattern = r'nvidia-(\d+)'
    # Find all matches for the pattern in the config string
    matches = re.findall(pattern, config['tags'])

    # Return the sorted list of matches
    return sorted(matches)


def parse_vgpu_bus_id(config) -> list:
    # Define the regular expression pattern
    pattern = r'-device vfio-pci,sysfsdev=(/sys/bus/pci/devices/[0-9a-fA-F:.]+)'

    # Search for the pattern in the config string
    matches = re.findall(pattern, config['args'])

    # If a match is found, extract and return the vGPU bus ID
    if not matches:
        return []

    return matches


def parse_vm_config(vmid, from_node):
    config_file = f'/etc/pve/qemu-server/{vmid}.conf'

    if from_node:
        config_file = f'/etc/pve/nodes/{from_node}/qemu-server/{vmid}.conf'

    try:
        with open(config_file) as file:
            config = file.read()
    except FileNotFoundError:
        print(f"VM {vmid} not found")
        sys.exit(1)

    # Split each string into a dict
    config_dict = {}
    for line in config.splitlines():
        if not line or line.startswith('#') or line.startswith('['):
            continue
        key, value = line.split(': ')
        config_dict[key] = value

    return config_dict


def parse_line_config(config_line, item):
    line_dict = {}
    for line in config_line.split(','):
        key, value = line.split('=')
        line_dict[key] = value

    return line_dict.get(item, None)

def assign_gpu(vmid, node, gpu, gpu_type):
    config_file = f'/etc/pve/nodes/{node}/qemu-server/{vmid}.conf'
    # Select from VMS
    vms = get_vms()
    config = next((vm[2] for vm in vms if vm[0] == vmid), None)
    if not config:
        sys.exit(1)

    arguments = []
    if 'args' in config and config['args']:
        arguments = config['args'].split(' ')

    # Make sure we do not add the same device twice
    if f'vfio-pci,sysfsdev=/sys/bus/pci/devices/{gpu}' in arguments:
        return

    arguments.append(f'-device vfio-pci,sysfsdev=/sys/bus/pci/devices/{gpu}')

    if '-uuid' not in arguments:
        uuid = parse_line_config(config['smbios1'], 'uuid')
        arguments.append(f'-uuid {uuid}')

    config['args'] = ' '.join(arguments)

    if 'hookscript' not in config or not config['hookscript']:
        config['hookscript'] = 'local:snippets/nvidia_allocator.py'

    tags = set()
    if 'tags' in config and config['tags']:
        # Parse tags
        tags = set(filter(None, config['tags'].strip().split(';')))

    tags.add(f"nvidia-{gpu_type}")
    config['tags'] = ';'.join(sorted(tags))

    with open(config_file, 'w') as file:
        for key, value in config.items():
            file.write(f'{key}: {value}\n')

    sys.exit(0)

def menu(stdscr: curses.window):
    curses.curs_set(0)
    stdscr.clear()
    vms = get_vms()
    current_row = 0

    while True:
        stdscr.clear()
        stdscr.addstr(0, 0, "Select a VM to assign/remove GPU:")
        for idx, (vmid, node, config) in enumerate(vms):
            x = 0
            y = idx + 1
            if idx == current_row:
                stdscr.addstr(y, x, f"{vmid} - {config['name']} - ({node})", A_STANDOUT)
            else:
                stdscr.addstr(y, x, f"{vmid} - {config['name']} - ({node})")
        stdscr.refresh()

        key = stdscr.getch()

        if key == curses.KEY_UP and current_row > 0:
            current_row -= 1
        elif key == curses.KEY_DOWN and current_row < len(vms) - 1:
            current_row += 1
        elif key == curses.KEY_ENTER or key in [10, 13]:
            vmid, node, _ = vms[current_row]
            gpu_type_menu(stdscr, vmid, node)
        elif key == 27:  # ESC key
            break

def gpu_type_menu(stdscr, vmid, node):
    curses.curs_set(0)
    stdscr.clear()
    _, _, gpu_types = get_gpus()
    gpu_types = list(gpu_types)
    gpu_types.sort()
    current_row = 0
    while True:
        stdscr.clear()
        stdscr.addstr(0, 0, f"VM {vmid} ({node}) - Select a GPU type:")
        for idx, gpu_type in enumerate(gpu_types):
            x = 0
            y = idx + 1
            if idx == current_row:
                stdscr.addstr(y, x, gpu_type, A_STANDOUT)
            else:
                stdscr.addstr(y, x, gpu_type)
        stdscr.refresh()
        key = stdscr.getch()
        if key == curses.KEY_UP and current_row > 0:
            current_row -= 1
        elif key == curses.KEY_DOWN and current_row < len(gpu_types) - 1:
            current_row += 1
        elif key == curses.KEY_ENTER or key in [10, 13]:
            selected_gpu_type = gpu_types[current_row].split(" : ")[0].strip()
            gpu_menu(stdscr, vmid, node, selected_gpu_type)
        elif key == 27:  # ESC key
            break

def gpu_menu(stdscr, vmid, node, selected_gpu_type):
    curses.curs_set(0)
    stdscr.clear()
    available_gpus, assigned_gpus, _ = get_gpus()
    filtered_gpus = {gpu: types for gpu, types in available_gpus.items() if selected_gpu_type in types}
    current_row = 0
    while True:
        stdscr.clear()
        stdscr.addstr(0, 0, f"VM {vmid} ({node}) - Select a GPU to assign:")
        for idx, gpu in enumerate(filtered_gpus):
            x = 0
            y = idx + 1
            if idx == current_row:
                stdscr.addstr(y, x, gpu, A_STANDOUT)
            else:
                stdscr.addstr(y, x, gpu)
        stdscr.refresh()
        key = stdscr.getch()
        if key == curses.KEY_UP and current_row > 0:
            current_row -= 1
        elif key == curses.KEY_DOWN and current_row < len(filtered_gpus) - 1:
            current_row += 1
        elif key == curses.KEY_ENTER or key in [10, 13]:
            selected_gpu = list(filtered_gpus.keys())[current_row]
            assign_gpu(vmid, node, selected_gpu, selected_gpu_type)
            # If we return, we are not able to assign the GPU, write an error, then go back
            stdscr.clear()
            stdscr.addstr(5, 5, "This GPU already assigned to this VM, press the any key to continue")
            stdscr.refresh()
            stdscr.getch()
        elif key == 27:  # ESC key
            break

def print_usage():
    print("Usage: script.py <vmid> <phase>")
    print("       script.py <vmid> get_command <vgpu_name>")
    print("       script.py menu")
    sys.exit(1)

def main():
    if len(sys.argv) < 2:
        print_usage()

    vmid = sys.argv[1]

    if vmid == "menu":
        # Initiate the menu
        curses.wrapper(menu)
        sys.exit(0)

    if len(sys.argv) < 3:
        print_usage()

    phase = sys.argv[2]
    if phase == "get_command":
        if len(sys.argv) < 4:
            print("Usage: script.py <vmid> get_command <vgpu_name>")
            sys.exit(1)
        vgpu_name = sys.argv[3]

    # Read the VM config file
    from_node = os.environ.get("PVE_MIGRATED_FROM", None)
    config_dict = parse_vm_config(vmid, from_node)

    if phase == 'get_command':
        available_vgpu, gpu_id = get_available_gpu(vgpu_name)
        uuid = parse_line_config(config_dict['smbios1'], 'uuid')
        print(f"qm set {vmid} --hookscript local:snippets/nvidia_allocator.py")
        print(
            f"qm set {vmid} --args \"-device vfio-pci,sysfsdev=/sys/bus/pci/devices/{available_vgpu} -uuid {uuid}\"")
        tags = set(filter(None, config_dict.get('tags', '').strip().split(';')))
        tags.add(f"nvidia-{gpu_id}")
        print(f"qm set {vmid} --tags \"{';'.join(sorted(tags))}\"")
        sys.exit(0)

    # Get the vGPU we want from config
    vgpu_types = parse_vgpu_type_id(config_dict)
    if not vgpu_types:
        # VM doesn't seem to require a GPU
        sys.exit(0)

    vgpu_paths = parse_vgpu_bus_id(config_dict)
    if not vgpu_paths:
        # No vGPU location specified
        sys.exit(0)

    if phase == 'pre-start':
        # Check if path already exists
        for vgpu_path in vgpu_paths:
            if not os.path.exists(vgpu_path):
                print(f"Specified vGPU not found, rerun the nvidia_allocator or check the drivers: {vgpu_path}")
                sys.exit(1)

            stop(vgpu_path)
        # We break the loop here so that if we misconfigured #2, then we stop before configuring the first

        for i, vgpu_path in enumerate(vgpu_paths):
            vgpu_type = vgpu_types[i % len(vgpu_types)]

            with open(f'{vgpu_path}/nvidia/current_vgpu_type', 'w') as file:
                file.write(vgpu_type)
                # Let Python handle any Exceptions here, so it crashes out with information

    if phase == 'post-stop':
        for vgpu_path in vgpu_paths:
            stop(vgpu_path)


def stop(vgpu_path):
    # Write 0 to current_vgpu_type to indicate that the vGPU is no longer in use
    try:
        with open(f'{vgpu_path}/nvidia/current_vgpu_type', 'w') as file:
            file.write('0')
    except (FileNotFoundError, PermissionError):
        # The vGPU path does not exist, so we can ignore this error
        print("vGPU already de-allocated")


if __name__ == "__main__":
    main()
    #  Make sure we exit with a 0 status code
    sys.exit(0)
