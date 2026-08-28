import unittest

from clevo_hid_probe import (
    HID_CLASS_GUID,
    LIGHTING_USAGE_HARDWARE_ID,
    PnpIdentity,
    evaluate_clevo_hid_identity,
    evaluate_hid_report_capabilities,
)


USB_ID = r"USB\VID_048D&PID_8910\5&36133734&0&8"
HID_ID = r"HID\VID_048D&PID_8910&COL02\6&26FA685D&0&0001"


def usb_identity(**changes):
    values = dict(
        instance_id=USB_ID,
        hardware_ids=(r"USB\VID_048D&PID_8910&REV_0001", r"USB\VID_048D&PID_8910"),
        service="HidUsb",
        class_guid=HID_CLASS_GUID,
        driver_key=HID_CLASS_GUID + r"\0000",
        config_flags=0,
        location_information="Port_#0008.Hub_#0002",
        present=True,
        parent_instance_id=r"USB\ROOT_HUB30\4&3867e3dc&0&0",
    )
    values.update(changes)
    return PnpIdentity(**values)


def lighting_identity(**changes):
    values = dict(
        instance_id=HID_ID,
        hardware_ids=(
            r"HID\VID_048D&PID_8910&REV_0001&Col02",
            LIGHTING_USAGE_HARDWARE_ID,
        ),
        service="",
        class_guid=HID_CLASS_GUID,
        driver_key=HID_CLASS_GUID + r"\0002",
        config_flags=0,
        location_information="",
        present=True,
        parent_instance_id=USB_ID,
    )
    values.update(changes)
    return PnpIdentity(**values)


class ClevoHidIdentityTests(unittest.TestCase):
    def test_exact_identity_matches_but_never_enables_writes(self):
        result = evaluate_clevo_hid_identity(
            [usb_identity()],
            [lighting_identity()],
        )

        self.assertTrue(result.identity_matched)
        self.assertFalse(result.writes_enabled)
        self.assertIn("remain unqualified", result.reason)

    def test_duplicate_usb_device_fails_closed(self):
        result = evaluate_clevo_hid_identity(
            [usb_identity(), usb_identity()],
            [lighting_identity()],
        )

        self.assertFalse(result.identity_matched)
        self.assertFalse(result.writes_enabled)

    def test_six_byte_descriptor_matches_but_writes_remain_disabled(self):
        identity = evaluate_clevo_hid_identity(
            [usb_identity()],
            [lighting_identity()],
        )
        result = evaluate_hid_report_capabilities(
            identity,
            [r"\\?\hid#vid_048d&pid_8910&col02#example"],
            vendor_id=0x048D,
            product_id=0x8910,
            version_number=1,
            usage_page=0xFF89,
            usage=0x00CC,
            input_report_bytes=0,
            output_report_bytes=0,
            feature_report_bytes=6,
        )

        self.assertTrue(result.descriptor_matched)
        self.assertFalse(result.writes_enabled)
        self.assertEqual(result.feature_report_bytes, 6)

    def test_ambiguous_interface_paths_fail_closed(self):
        identity = evaluate_clevo_hid_identity(
            [usb_identity()],
            [lighting_identity()],
        )
        result = evaluate_hid_report_capabilities(
            identity,
            ["one", "two"],
            vendor_id=None,
            product_id=None,
            version_number=None,
            usage_page=None,
            usage=None,
            input_report_bytes=None,
            output_report_bytes=None,
            feature_report_bytes=None,
        )

        self.assertFalse(result.descriptor_matched)
        self.assertFalse(result.writes_enabled)

    def test_wrong_parent_fails_closed(self):
        result = evaluate_clevo_hid_identity(
            [usb_identity()],
            [lighting_identity(parent_instance_id=r"USB\VID_0000&PID_0000\OTHER")],
        )

        self.assertFalse(result.identity_matched)
        self.assertFalse(result.writes_enabled)

    def test_absent_or_wrong_usage_collection_fails_closed(self):
        result = evaluate_clevo_hid_identity(
            [usb_identity()],
            [lighting_identity(hardware_ids=(r"HID\VID_048D&UP:FF89_U:00CE",))],
        )

        self.assertFalse(result.identity_matched)
        self.assertFalse(result.writes_enabled)


if __name__ == "__main__":
    unittest.main()
