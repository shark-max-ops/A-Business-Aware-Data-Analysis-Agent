"""服务端固定的来源、站点和机场映射，不接受用户网址或猜测站点。"""

from dataclasses import asdict, dataclass
from datetime import date
from math import asin, cos, radians, sin, sqrt
from urllib.parse import urlsplit

from metric_pilot.data.common import DataError

NCEI_ROOT = "https://www.ncei.noaa.gov/oa/global-historical-climatology-network"
AWC_ROOT = "https://aviationweather.gov/api/data"
FAA_ROOT = "https://www.fly.faa.gov/adv"
PUBLIC_SOURCE_HOSTS = frozenset({"aviationweather.gov", "www.ncei.noaa.gov", "www.fly.faa.gov"})
SOURCE_POLICIES = {
    "awc_metar": {
        "enabled": True,
        "operator": "NOAA / NWS Aviation Weather Center",
        "terms": ["https://aviationweather.gov/data/api/", "https://www.weather.gov/disclaimer"],
        "checked_on": "2026-11-03",
        "authentication": "anonymous",
        "fee": "公开接口无需购买数据服务",
        "provider_limit": "100 requests/minute；多数接口最多 400 条；最近 30 天",
        "local_limit": "单查询最多 8 次请求、间隔至少 2 秒、不自动重试",
        "attribution": "NOAA/NWS AWC 原始观测；项目转换不代表官方产品或背书",
        "sent": "单个已登记 ICAO、UTC 终点和向前小时数、json 格式",
    },
    "ncei_ghcnh": {
        "enabled": True,
        "operator": "NOAA / NCEI",
        "terms": [
            "https://www.ncei.noaa.gov/metadata/geoportal/rest/metadata/item/"
            "gov.noaa.ncdc:C01688/html",
            "https://data.ngdc.noaa.gov/ngdcinfo/privacy.html",
            NCEI_ROOT + "/hourly/doc/ghcnh_DOCUMENTATION.pdf",
        ],
        "checked_on": "2026-11-03",
        "authentication": "anonymous",
        "fee": "公开电子文件免费；不使用认证或定制服务",
        "provider_limit": "未公布该文件入口数字配额；不将未知解释为无限访问",
        "local_limit": "单查询最多两个年度文件、每份最多 64 MiB、间隔至少 2 秒",
        "attribution": "Menne et al. (2023), GHCNh, doi:10.25921/jp3d-3v19；标注站点/日期/获取时间",
        "sent": "已登记站点编号与年度文件路径",
    },
    "faa_atcscc": {
        "enabled": True,
        "operator": "FAA Air Traffic Control System Command Center",
        "terms": [
            "https://www.faa.gov/web_policies/disclaimer",
            "https://www.fly.faa.gov/adv/advAdvisoryForm",
            "https://www.faa.gov/air_traffic/publications/atpubs/aim_html/chap5_section_1.html",
        ],
        "checked_on": "2026-11-03",
        "authentication": "公开查询入口，匿名只读",
        "fee": "不购买数据服务；仅公开清单与正文",
        "provider_limit": "未公布该网页入口数字配额或稳定 API 承诺",
        "local_limit": "每次最多三天/三次清单或一次正文；间隔至少两秒、不自动重试",
        "attribution": "FAA ATCSCC 公告原文；保留编号、日期与出处；项目标签不冒充官方解释",
        "use_scope": "仅本机低频查阅、留存与引用；不作公开镜像或批量再分发授权",
        "restriction": "拒绝登录/封锁/跳转和脚本挑战；不抓加拿大公告、图片或第三方链接",
        "licensing_boundary": "未取得通用自动抓取许可证；公开查阅不等于无限抓取/再分发",
        "sent": "UTC 日期与 ATCSCC/All 固定条件；正文仅清单登记的日期和编号",
    },
}


@dataclass(frozen=True)
class AirportStation:
    """
    作用：保存经官方机场、站点和历史观测交叉核对的有限映射。
    输入：机场和站点标识、坐标、时区及适用日期。
    输出：不可变映射。
    """

    airport: str
    icao: str
    station: str
    timezone: str
    airport_lat: float
    airport_lon: float
    station_lat: float
    station_lon: float
    valid_from: date = date(2026, 1, 1)
    valid_to: date = date(2026, 12, 31)

    def detail(self) -> dict:
        """
        作用：公开映射依据、距离和历史边界。
        输入：无。
        输出：可 JSON 编码的站点说明。
        """
        result = asdict(self)
        result.update(
            valid_from=self.valid_from.isoformat(),
            valid_to=self.valid_to.isoformat(),
            distance_km=round(
                distance_km(self.airport_lat, self.airport_lon, self.station_lat, self.station_lon),
                3,
            ),
            airport_source=AWC_ROOT + f"/airport?ids={self.icao}&format=json",
            station_source=AWC_ROOT + f"/stationinfo?ids={self.icao}&format=json",
            historical_source=NCEI_ROOT + "/hourly/doc/ghcnh-station-list.txt",
            mapping_review="2026-11-03；机场接口 source=FAA；历史观测逐行核对坐标与 ICAO",
            limitation="仅登记三站及 2026 年；未取得完整迁站历史，坐标/标识不匹配即拒绝",
        )
        return result


# 坐标来自实际官方响应；ICAO/IATA 由接口明确对应，不通过前缀推导。
STATIONS = (
    AirportStation(
        "ATL", "KATL", "USW00013874", "America/New_York", 33.6367, -84.4279, 33.62972, -84.44223
    ),
    AirportStation(
        "ORD", "KORD", "USW00094846", "America/Chicago", 41.9769, -87.9081, 41.96017, -87.93161
    ),
    AirportStation(
        "JFK", "KJFK", "USW00094789", "America/New_York", 40.6399, -73.7787, 40.63916, -73.76394
    ),
)


def distance_km(lat1, lon1, lat2, lon2) -> float:
    """
    作用：计算两点球面距离以核对站点位置。
    输入：两组经纬度。
    输出：公里距离。
    """
    a = sin(radians(lat2 - lat1) / 2) ** 2
    a += cos(radians(lat1)) * cos(radians(lat2)) * sin(radians(lon2 - lon1) / 2) ** 2
    return 6371.0088 * 2 * asin(min(1, sqrt(a)))


def resolve_station(airport: str, start: date, end: date, stations=STATIONS) -> AirportStation:
    """
    作用：只允许日期适用且唯一的已核对站点。
    输入：机场代码、当地起止日期、服务端映射集合。
    输出：映射；不存在、过期或歧义时抛出安全错误。
    """
    candidates = [s for s in stations if airport in (s.airport, s.icao)]
    if not candidates:
        raise DataError("WEATHER_STATION_NOT_FOUND")
    candidates = [s for s in candidates if s.valid_from <= start <= end <= s.valid_to]
    if len(candidates) != 1:
        raise DataError("WEATHER_STATION_AMBIGUOUS" if candidates else "WEATHER_MAPPING_DATE")
    return candidates[0]


def validate_url(url: str) -> str:
    """
    作用：限定只读来源和精确路径；全部跳转由请求层拒绝。
    输入：服务端构造的 URL。
    输出：主机名；越界抛错。
    """
    import re

    part = urlsplit(url)
    allowed = (
        (part.hostname == "www.fly.faa.gov" and part.path in ("/adv/adv_list", "/adv/adv_otherdis"))
        or (part.hostname == "aviationweather.gov" and part.path == "/api/data/metar")
        or (
            part.hostname == "www.ncei.noaa.gov"
            and re.fullmatch(
                r"/oa/global-historical-climatology-network/hourly/access/by-year/"
                r"20\d{2}/psv/GHCNh_USW\d{8}_20\d{2}\.psv",
                part.path,
            )
        )
    )
    if (
        part.scheme != "https"
        or part.username
        or part.password
        or part.fragment
        or part.query
        or part.netloc != part.hostname
        or not allowed
    ):
        raise DataError("EXTERNAL_ADDRESS_REJECTED")
    return part.hostname
