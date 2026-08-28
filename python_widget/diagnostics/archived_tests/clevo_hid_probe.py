"""Read-only identity probe for the PowerSpec 1520 keyboard HID collection.

This module never opens a HID handle and never sends an input, output, or
feature report.  It reads Windows Plug and Play identity data only.  A match
does not qualify any lighting write protocol; it merely proves that the exact
device collection expected by later research is present and parented to the
expected USB device.
"""

from __future__ import annotations

import ctypes
import json
import os
from dataclasses import asdict, dataclass
from typing import Iterable


HID_CLASS_GUID = "{745a17a0-74d3-11d0-b6fe-00a0c90f57da}"
USB_DEVICE_PREFIX = "USB\\VID_048D&PID_8910\\"
HID_COLLECTION_PREFIX = "HID\\VID_048D&PID_8910&COL02\\"
LIGHTING_USAGE_HARDWARE_ID = r"HID\VID_048D&UP:FF89_U:00CC"
EXPECTED_USB_LOCATION = "Port_#0008"


@dataclass(frozen=True)
class PnpIdentity:
    instance_id: str
    hardware_ids: tuple[str, ...]
    service: str
    class_guid: str
    driver_key: str
    config_flags: int
    location_information: str
    present: bool
    parent_instance_id: str | None


@dataclass(frozen=True)
class ClevoHidIdentityProbe:
    identity_matched: bool
    writes_enabled: bool
    reason: str
    usb_device: PnpIdentity | None
    lighting_collection: PnpIdentity | None


@dataclass(frozen=True)
class HidReportCapabilityProbe:
    descriptor_matched: bool
    writes_enabled: bool
    reason: str
    interface_path: str | None
    vendor_id: int | None
    product_id: int | None
    version_number: int | None
    usage_page: int | None
    usage: int | None
    input_report_bytes: int | None
    output_report_bytes: int | None
    feature_report_bytes: int | None


def _normalized(value: str) -> str:
    return value.strip().casefold()


def evaluate_clevo_hid_identity(
    usb_devices: Iterable[PnpIdentity],
    hid_collections: Iterable[PnpIdentity],
) -> ClevoHidIdentityProbe:
    """Validate the immutable PnP identity while keeping all writes disabled."""

    usb_matches = [
        record
        for record in usb_devices
        if _normalized(record.instance_id).startswith(_normalized(USB_DEVICE_PREFIX))
        and any(
            _normalized(hardware_id) == _normalized(r"USB\VID_048D&PID_8910")
            for hardware_id in record.hardware_ids
        )
    ]
    if len(usb_matches) != 1:
        return ClevoHidIdentityProbe(
            False,
            False,
            f"Expected one 048D:8910 USB device; found {len(usb_matches)}.",
            None,
            None,
        )

    usb_device = usb_matches[0]
    usb_ok = (
        usb_device.present
        and _normalized(usb_device.service) == "hidusb"
        and _normalized(usb_device.class_guid) == _normalized(HID_CLASS_GUID)
        and bool(usb_device.driver_key.strip())
        and usb_device.config_flags == 0
        and EXPECTED_USB_LOCATION.casefold()
        in usb_device.location_information.casefold()
    )
    if not usb_ok:
        return ClevoHidIdentityProbe(
            False,
            False,
            "The 048D:8910 USB device does not match the read-only driver/location allowlist.",
            usb_device,
            None,
        )

    collection_matches = [
        record
        for record in hid_collections
        if _normalized(record.instance_id).startswith(
            _normalized(HID_COLLECTION_PREFIX)
        )
        and any(
            _normalized(hardware_id) == _normalized(LIGHTING_USAGE_HARDWARE_ID)
            for hardware_id in record.hardware_ids
        )
    ]
    if len(collection_matches) != 1:
        return ClevoHidIdentityProbe(
            False,
            False,
            f"Expected one FF89/00CC lighting collection; found {len(collection_matches)}.",
            usb_device,
            None,
        )

    collection = collection_matches[0]
    collection_ok = (
        collection.present
        and _normalized(collection.class_guid) == _normalized(HID_CLASS_GUID)
        and bool(collection.driver_key.strip())
        and collection.config_flags == 0
        and _normalized(collection.parent_instance_id or "")
        == _normalized(usb_device.instance_id)
    )
    if not collection_ok:
        return ClevoHidIdentityProbe(
            False,
            False,
            "The lighting collection is absent, unhealthy, or attached to the wrong parent.",
            usb_device,
            collection,
        )

    return ClevoHidIdentityProbe(
        True,
        False,
        "Exact HID identity matched. Write protocol, report length, acknowledgement, "
        "readback, and restoration remain unqualified, so writes stay disabled.",
        usb_device,
        collection,
    )


def evaluate_hid_report_capabilities(
    identity: ClevoHidIdentityProbe,
    interface_paths: Iterable[str],
    *,
    vendor_id: int | None,
    product_id: int | None,
    version_number: int | None,
    usage_page: int | None,
    usage: int | None,
    input_report_bytes: int | None,
    output_report_bytes: int | None,
    feature_report_bytes: int | None,
) -> HidReportCapabilityProbe:
    paths = tuple(interface_paths)
    path = paths[0] if len(paths) == 1 else None
    common = dict(
        writes_enabled=False,
        interface_path=path,
        vendor_id=vendor_id,
        product_id=product_id,
        version_number=version_number,
        usage_page=usage_page,
        usage=usage,
        input_report_bytes=input_report_bytes,
        output_report_bytes=output_report_bytes,
        feature_report_bytes=feature_report_bytes,
    )
    if not identity.identity_matched:
        return HidReportCapabilityProbe(
            descriptor_matched=False,
            reason="PnP identity did not match, so descriptor evidence was rejected.",
            **common,
        )
    if len(paths) != 1:
        return HidReportCapabilityProbe(
            descriptor_matched=False,
            reason=f"Expected one COL02 interface path; found {len(paths)}.",
            **common,
        )
    if (
        vendor_id != 0x048D
        or product_id != 0x8910
        or usage_page != 0xFF89
        or usage != 0x00CC
        or feature_report_bytes not in {6, 7}
    ):
        return HidReportCapabilityProbe(
            descriptor_matched=False,
            reason="HID attributes, usage, or feature-report length did not match the research allowlist.",
            **common,
        )
    return HidReportCapabilityProbe(
        descriptor_matched=True,
        reason=(
            f"Descriptor confirms {feature_report_bytes}-byte feature reports including "
            "the report ID. No report was read or written; acknowledgement, semantic "
            "readback, and restoration remain unqualified."
        ),
        **common,
    )


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("InterfaceClassGuid", _GUID),
        ("Flags", ctypes.c_ulong),
        ("Reserved", ctypes.c_size_t),
    ]


class _HIDD_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("Size", ctypes.c_ulong),
        ("VendorID", ctypes.c_ushort),
        ("ProductID", ctypes.c_ushort),
        ("VersionNumber", ctypes.c_ushort),
    ]


class _HIDP_CAPS(ctypes.Structure):
    _fields_ = [
        ("Usage", ctypes.c_ushort),
        ("UsagePage", ctypes.c_ushort),
        ("InputReportByteLength", ctypes.c_ushort),
        ("OutputReportByteLength", ctypes.c_ushort),
        ("FeatureReportByteLength", ctypes.c_ushort),
        ("Reserved", ctypes.c_ushort * 17),
        ("NumberLinkCollectionNodes", ctypes.c_ushort),
        ("NumberInputButtonCaps", ctypes.c_ushort),
        ("NumberInputValueCaps", ctypes.c_ushort),
        ("NumberInputDataIndices", ctypes.c_ushort),
        ("NumberOutputButtonCaps", ctypes.c_ushort),
        ("NumberOutputValueCaps", ctypes.c_ushort),
        ("NumberOutputDataIndices", ctypes.c_ushort),
        ("NumberFeatureButtonCaps", ctypes.c_ushort),
        ("NumberFeatureValueCaps", ctypes.c_ushort),
        ("NumberFeatureDataIndices", ctypes.c_ushort),
    ]


def _enumerate_lighting_interface_paths() -> list[str]:
    hid = ctypes.WinDLL("hid", use_last_error=True)
    setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
    hid_guid = _GUID()
    hid.HidD_GetHidGuid.argtypes = [ctypes.POINTER(_GUID)]
    hid.HidD_GetHidGuid(ctypes.byref(hid_guid))

    setupapi.SetupDiGetClassDevsW.argtypes = [
        ctypes.POINTER(_GUID),
        ctypes.c_wchar_p,
        ctypes.c_void_p,
        ctypes.c_ulong,
    ]
    setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
    device_info_set = setupapi.SetupDiGetClassDevsW(
        ctypes.byref(hid_guid),
        None,
        None,
        0x00000002 | 0x00000010,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if device_info_set == invalid_handle:
        raise ctypes.WinError(ctypes.get_last_error())

    setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(_GUID),
        ctypes.c_ulong,
        ctypes.POINTER(_SP_DEVICE_INTERFACE_DATA),
    ]
    setupapi.SetupDiEnumDeviceInterfaces.restype = ctypes.c_bool
    setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_SP_DEVICE_INTERFACE_DATA),
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.c_void_p,
    ]
    setupapi.SetupDiGetDeviceInterfaceDetailW.restype = ctypes.c_bool
    setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
    setupapi.SetupDiDestroyDeviceInfoList.restype = ctypes.c_bool

    paths: list[str] = []
    try:
        index = 0
        while True:
            interface_data = _SP_DEVICE_INTERFACE_DATA()
            interface_data.cbSize = ctypes.sizeof(_SP_DEVICE_INTERFACE_DATA)
            if not setupapi.SetupDiEnumDeviceInterfaces(
                device_info_set,
                None,
                ctypes.byref(hid_guid),
                index,
                ctypes.byref(interface_data),
            ):
                error = ctypes.get_last_error()
                if error == 259:
                    break
                raise ctypes.WinError(error)
            index += 1

            required_size = ctypes.c_ulong()
            setupapi.SetupDiGetDeviceInterfaceDetailW(
                device_info_set,
                ctypes.byref(interface_data),
                None,
                0,
                ctypes.byref(required_size),
                None,
            )
            if required_size.value == 0:
                raise ctypes.WinError(ctypes.get_last_error())
            detail_buffer = ctypes.create_string_buffer(required_size.value)
            detail_cb_size = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
            ctypes.cast(detail_buffer, ctypes.POINTER(ctypes.c_ulong))[0] = detail_cb_size
            if not setupapi.SetupDiGetDeviceInterfaceDetailW(
                device_info_set,
                ctypes.byref(interface_data),
                detail_buffer,
                required_size.value,
                None,
                None,
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            path = ctypes.wstring_at(
                ctypes.addressof(detail_buffer) + ctypes.sizeof(ctypes.c_ulong)
            )
            lowered = path.casefold()
            if "vid_048d&pid_8910" in lowered and "col02" in lowered:
                paths.append(path)
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(device_info_set)
    return paths


def _read_hid_report_capabilities(
    interface_path: str,
) -> tuple[int, int, int, int, int, int, int, int]:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    hid = ctypes.WinDLL("hid", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_void_p,
    ]
    kernel32.CreateFileW.restype = ctypes.c_void_p
    handle = kernel32.CreateFileW(
        interface_path,
        0,
        0x00000001 | 0x00000002,
        None,
        3,
        0,
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())

    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool
    preparsed_data = ctypes.c_void_p()
    try:
        attributes = _HIDD_ATTRIBUTES()
        attributes.Size = ctypes.sizeof(_HIDD_ATTRIBUTES)
        hid.HidD_GetAttributes.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_HIDD_ATTRIBUTES),
        ]
        hid.HidD_GetAttributes.restype = ctypes.c_bool
        if not hid.HidD_GetAttributes(handle, ctypes.byref(attributes)):
            raise ctypes.WinError(ctypes.get_last_error())

        hid.HidD_GetPreparsedData.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        hid.HidD_GetPreparsedData.restype = ctypes.c_bool
        if not hid.HidD_GetPreparsedData(handle, ctypes.byref(preparsed_data)):
            raise ctypes.WinError(ctypes.get_last_error())

        caps = _HIDP_CAPS()
        hid.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.POINTER(_HIDP_CAPS)]
        hid.HidP_GetCaps.restype = ctypes.c_long
        status = hid.HidP_GetCaps(preparsed_data, ctypes.byref(caps))
        if status & 0xFFFFFFFF != 0x00110000:
            raise RuntimeError(f"HidP_GetCaps failed with NTSTATUS 0x{status & 0xFFFFFFFF:08X}")
        return (
            attributes.VendorID,
            attributes.ProductID,
            attributes.VersionNumber,
            caps.UsagePage,
            caps.Usage,
            caps.InputReportByteLength,
            caps.OutputReportByteLength,
            caps.FeatureReportByteLength,
        )
    finally:
        if preparsed_data.value:
            hid.HidD_FreePreparsedData.argtypes = [ctypes.c_void_p]
            hid.HidD_FreePreparsedData.restype = ctypes.c_bool
            hid.HidD_FreePreparsedData(preparsed_data)
        kernel32.CloseHandle(handle)


def probe_clevo_hid_report_capabilities(
    identity: ClevoHidIdentityProbe | None = None,
) -> HidReportCapabilityProbe:
    """Read descriptor metadata without requesting or sending any HID report."""

    if identity is None:
        identity = probe_clevo_hid_identity()
    if os.name != "nt" or not identity.identity_matched:
        return evaluate_hid_report_capabilities(
            identity,
            (),
            vendor_id=None,
            product_id=None,
            version_number=None,
            usage_page=None,
            usage=None,
            input_report_bytes=None,
            output_report_bytes=None,
            feature_report_bytes=None,
        )
    try:
        paths = _enumerate_lighting_interface_paths()
        if len(paths) != 1:
            return evaluate_hid_report_capabilities(
                identity,
                paths,
                vendor_id=None,
                product_id=None,
                version_number=None,
                usage_page=None,
                usage=None,
                input_report_bytes=None,
                output_report_bytes=None,
                feature_report_bytes=None,
            )
        values = _read_hid_report_capabilities(paths[0])
        return evaluate_hid_report_capabilities(
            identity,
            paths,
            vendor_id=values[0],
            product_id=values[1],
            version_number=values[2],
            usage_page=values[3],
            usage=values[4],
            input_report_bytes=values[5],
            output_report_bytes=values[6],
            feature_report_bytes=values[7],
        )
    except Exception as exc:
        return HidReportCapabilityProbe(
            descriptor_matched=False,
            writes_enabled=False,
            reason=f"Read-only HID descriptor probe failed: {type(exc).__name__}: {exc}",
            interface_path=None,
            vendor_id=None,
            product_id=None,
            version_number=None,
            usage_page=None,
            usage=None,
            input_report_bytes=None,
            output_report_bytes=None,
            feature_report_bytes=None,
        )


def _devnode_presence_and_parent(instance_id: str) -> tuple[bool, str | None]:
    cfgmgr32 = ctypes.WinDLL("cfgmgr32", use_last_error=True)
    devinst = ctypes.c_ulong()
    locate = cfgmgr32.CM_Locate_DevNodeW
    locate.argtypes = [ctypes.POINTER(ctypes.c_ulong), ctypes.c_wchar_p, ctypes.c_ulong]
    locate.restype = ctypes.c_ulong
    if locate(ctypes.byref(devinst), instance_id, 0) != 0:
        return False, None

    parent = ctypes.c_ulong()
    get_parent = cfgmgr32.CM_Get_Parent
    get_parent.argtypes = [
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.c_ulong,
        ctypes.c_ulong,
    ]
    get_parent.restype = ctypes.c_ulong
    if get_parent(ctypes.byref(parent), devinst.value, 0) != 0:
        return True, None

    size = ctypes.c_ulong()
    get_size = cfgmgr32.CM_Get_Device_ID_Size
    get_size.argtypes = [
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.c_ulong,
        ctypes.c_ulong,
    ]
    get_size.restype = ctypes.c_ulong
    if get_size(ctypes.byref(size), parent.value, 0) != 0:
        return True, None

    buffer = ctypes.create_unicode_buffer(size.value + 1)
    get_id = cfgmgr32.CM_Get_Device_IDW
    get_id.argtypes = [
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_wchar),
        ctypes.c_ulong,
        ctypes.c_ulong,
    ]
    get_id.restype = ctypes.c_ulong
    if get_id(parent.value, buffer, len(buffer), 0) != 0:
        return True, None
    return True, buffer.value


def _read_enum_records(bus: str, device_prefix: str) -> list[PnpIdentity]:
    import winreg

    records: list[PnpIdentity] = []
    enum_path = rf"SYSTEM\CurrentControlSet\Enum\{bus}"
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, enum_path) as enum_key:
        device_index = 0
        while True:
            try:
                device_name = winreg.EnumKey(enum_key, device_index)
            except OSError:
                break
            device_index += 1
            if not device_name.casefold().startswith(device_prefix.casefold()):
                continue

            with winreg.OpenKey(enum_key, device_name) as device_key:
                instance_index = 0
                while True:
                    try:
                        instance_name = winreg.EnumKey(device_key, instance_index)
                    except OSError:
                        break
                    instance_index += 1
                    with winreg.OpenKey(device_key, instance_name) as instance_key:
                        values: dict[str, object] = {}
                        value_index = 0
                        while True:
                            try:
                                name, value, _ = winreg.EnumValue(instance_key, value_index)
                            except OSError:
                                break
                            value_index += 1
                            values[name] = value

                    instance_id = rf"{bus}\{device_name}\{instance_name}"
                    present, parent = _devnode_presence_and_parent(instance_id)
                    hardware_ids = values.get("HardwareID", ())
                    if isinstance(hardware_ids, str):
                        hardware_ids = (hardware_ids,)
                    records.append(
                        PnpIdentity(
                            instance_id=instance_id,
                            hardware_ids=tuple(str(value) for value in hardware_ids),
                            service=str(values.get("Service", "")),
                            class_guid=str(values.get("ClassGUID", "")),
                            driver_key=str(values.get("Driver", "")),
                            config_flags=int(values.get("ConfigFlags", 0)),
                            location_information=str(
                                values.get("LocationInformation", "")
                            ),
                            present=present,
                            parent_instance_id=parent,
                        )
                    )
    return records


def probe_clevo_hid_identity() -> ClevoHidIdentityProbe:
    """Collect and evaluate OS-visible identity facts without opening HID."""

    if os.name != "nt":
        return ClevoHidIdentityProbe(
            False,
            False,
            "Clevo HID identity probing is supported only on Windows.",
            None,
            None,
        )
    try:
        usb_devices = _read_enum_records("USB", "VID_048D&PID_8910")
        hid_collections = _read_enum_records(
            "HID", "VID_048D&PID_8910&Col02"
        )
        return evaluate_clevo_hid_identity(usb_devices, hid_collections)
    except Exception as exc:
        return ClevoHidIdentityProbe(
            False,
            False,
            f"Read-only PnP probe failed: {type(exc).__name__}: {exc}",
            None,
            None,
        )


if __name__ == "__main__":
    identity_probe = probe_clevo_hid_identity()
    capability_probe = probe_clevo_hid_report_capabilities(identity_probe)
    print(
        json.dumps(
            {
                "identity": asdict(identity_probe),
                "capabilities": asdict(capability_probe),
            },
            indent=2,
        )
    )
