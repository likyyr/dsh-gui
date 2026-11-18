from enum import IntFlag

import comtypes.gen._00020430_0000_0000_C000_000000000046_0_2_0 as __wrapper_module__
from comtypes.gen._00020430_0000_0000_C000_000000000046_0_2_0 import (
    _check_version, GUID, typelib_path, FONTITALIC, FONTBOLD,
    VgaColor, Font, Library, Color, IPicture, FONTSIZE, Gray, IFont,
    OLE_XSIZE_HIMETRIC, IDispatch, IPictureDisp, OLE_COLOR,
    OLE_YPOS_CONTAINER, OLE_CANCELBOOL, OLE_OPTEXCLUSIVE, CoClass,
    OLE_HANDLE, IFontEventsDisp, OLE_YPOS_HIMETRIC, FontEvents,
    IUnknown, OLE_YSIZE_PIXELS, OLE_XPOS_PIXELS, DISPMETHOD,
    IEnumVARIANT, BSTR, OLE_XSIZE_PIXELS, FONTUNDERSCORE,
    OLE_ENABLEDEFAULTBOOL, dispid, EXCEPINFO, Picture,
    OLE_XPOS_CONTAINER, Unchecked, OLE_XSIZE_CONTAINER,
    OLE_YPOS_PIXELS, DISPPROPERTY, COMMETHOD, OLE_YSIZE_HIMETRIC,
    OLE_XPOS_HIMETRIC, IFontDisp, OLE_YSIZE_CONTAINER, Default,
    FONTSTRIKETHROUGH, StdPicture, Checked, StdFont, VARIANT_BOOL,
    Monochrome, _lcid, DISPPARAMS, FONTNAME, HRESULT
)


class OLE_TRISTATE(IntFlag):
    Unchecked = 0
    Checked = 1
    Gray = 2


class LoadPictureConstants(IntFlag):
    Default = 0
    Monochrome = 1
    VgaColor = 2
    Color = 4


__all__ = [
    'Picture', 'LoadPictureConstants', 'FONTITALIC',
    'OLE_XPOS_CONTAINER', 'FONTBOLD', 'Unchecked',
    'OLE_XSIZE_CONTAINER', 'VgaColor', 'Font', 'OLE_YPOS_PIXELS',
    'Library', 'Color', 'IPicture', 'FONTSIZE', 'Gray', 'IFont',
    'OLE_YSIZE_HIMETRIC', 'OLE_XSIZE_HIMETRIC', 'OLE_XPOS_HIMETRIC',
    'IFontDisp', 'IPictureDisp', 'OLE_YSIZE_CONTAINER', 'Default',
    'OLE_COLOR', 'OLE_TRISTATE', 'OLE_YPOS_CONTAINER',
    'FONTSTRIKETHROUGH', 'StdPicture', 'OLE_CANCELBOOL',
    'OLE_OPTEXCLUSIVE', 'Checked', 'StdFont', 'OLE_HANDLE',
    'IFontEventsDisp', 'OLE_YPOS_HIMETRIC', 'FontEvents', 'FONTNAME',
    'OLE_YSIZE_PIXELS', 'OLE_XPOS_PIXELS', 'Monochrome',
    'OLE_XSIZE_PIXELS', 'FONTUNDERSCORE', 'OLE_ENABLEDEFAULTBOOL',
    'typelib_path'
]

