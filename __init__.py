def classFactory(iface):
    from .geoiaz_plugin import GeoIAZPlugin
    return GeoIAZPlugin(iface)