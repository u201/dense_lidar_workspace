#include "pcd_writer.h"

#include <algorithm>
#include <cstdio>
#include <fstream>

namespace dense_lidar {
namespace {

// 每个字段一行：元信息 + ascii 格式化 + binary 取址。
// 新增字段只需在这里加一条，ascii / binary 两条写出路径共用，避免两份 switch 漂移。
struct FieldSpec {
  FieldInfo info;
  void (*FormatAscii)(const Point&, char* buf, size_t len);
  const void* (*Raw)(const Point&);
};

const std::vector<FieldSpec>& FieldTable() {
  static const std::vector<FieldSpec> kTable = {
      {{Field::kRing, "ring", 2, 'U'},
       [](const Point& p, char* buf, size_t len) {
         std::snprintf(buf, len, "%u", static_cast<unsigned>(p.ring));
       },
       [](const Point& p) -> const void* { return &p.ring; }},
      {{Field::kX, "x", 4, 'F'},
       [](const Point& p, char* buf, size_t len) { std::snprintf(buf, len, "%.9g", p.x); },
       [](const Point& p) -> const void* { return &p.x; }},
      {{Field::kY, "y", 4, 'F'},
       [](const Point& p, char* buf, size_t len) { std::snprintf(buf, len, "%.9g", p.y); },
       [](const Point& p) -> const void* { return &p.y; }},
      {{Field::kZ, "z", 4, 'F'},
       [](const Point& p, char* buf, size_t len) { std::snprintf(buf, len, "%.9g", p.z); },
       [](const Point& p) -> const void* { return &p.z; }},
      {{Field::kIntensity, "intensity", 1, 'U'},
       [](const Point& p, char* buf, size_t len) {
         std::snprintf(buf, len, "%u", static_cast<unsigned>(p.intensity));
       },
       [](const Point& p) -> const void* { return &p.intensity; }},
      {{Field::kTimestamp, "timestamp", 8, 'F'},
       // 时间戳通常是 1e9 量级的秒数，用 17 位有效数字避免精度损失。
       [](const Point& p, char* buf, size_t len) { std::snprintf(buf, len, "%.17g", p.timestamp); },
       [](const Point& p) -> const void* { return &p.timestamp; }},
      {{Field::kDistance, "distance", 4, 'F'},
       [](const Point& p, char* buf, size_t len) { std::snprintf(buf, len, "%.9g", p.distance); },
       [](const Point& p) -> const void* { return &p.distance; }},
      {{Field::kConfidence, "confidence", 1, 'U'},
       [](const Point& p, char* buf, size_t len) {
         std::snprintf(buf, len, "%u", static_cast<unsigned>(p.confidence));
       },
       [](const Point& p) -> const void* { return &p.confidence; }},
      {{Field::kAzimuthCalib, "azimuthCalib", 4, 'F'},
       [](const Point& p, char* buf, size_t len) {
         std::snprintf(buf, len, "%.9g", p.azimuthCalib);
       },
       [](const Point& p) -> const void* { return &p.azimuthCalib; }},
      {{Field::kElevationCalib, "elevationCalib", 4, 'F'},
       [](const Point& p, char* buf, size_t len) {
         std::snprintf(buf, len, "%.9g", p.elevationCalib);
       },
       [](const Point& p) -> const void* { return &p.elevationCalib; }},
  };
  return kTable;
}

const FieldSpec& SpecOf(Field field) {
  const std::vector<FieldSpec>& table = FieldTable();
  for (const FieldSpec& spec : table) {
    if (spec.info.field == field) {
      return spec;
    }
  }
  // Field 枚举值都被上表覆盖，这里不会到达。
  return table.front();
}

void AppendField(std::string* out, const Point& point, const FieldSpec& spec, bool binary) {
  if (binary) {
    out->append(static_cast<const char*>(spec.Raw(point)), spec.info.size);
    return;
  }
  char buf[64];
  spec.FormatAscii(point, buf, sizeof(buf));
  out->append(buf);
}

std::string Trim(const std::string& text) {
  const char* whitespace = " \t\r\n";
  size_t begin = text.find_first_not_of(whitespace);
  if (begin == std::string::npos) {
    return std::string();
  }
  size_t end = text.find_last_not_of(whitespace);
  return text.substr(begin, end - begin + 1);
}

}  // namespace

const FieldInfo& FindField(Field field) { return SpecOf(field).info; }

bool ParseFields(const std::string& text, std::vector<Field>* fields, std::string* error) {
  fields->clear();
  size_t pos = 0;
  while (pos <= text.size()) {
    size_t comma = text.find(',', pos);
    std::string name = Trim(text.substr(pos, comma == std::string::npos ? std::string::npos : comma - pos));
    if (!name.empty()) {
      bool found = false;
      for (const FieldSpec& spec : FieldTable()) {
        if (name == spec.info.name) {
          found = true;
          if (std::find(fields->begin(), fields->end(), spec.info.field) == fields->end()) {
            fields->push_back(spec.info.field);
          }
          break;
        }
      }
      if (!found) {
        std::string supported;
        for (const FieldSpec& spec : FieldTable()) {
          supported += supported.empty() ? spec.info.name : std::string(",") + spec.info.name;
        }
        if (error != nullptr) {
          *error = "unknown field '" + name + "', supported fields: " + supported;
        }
        return false;
      }
    }
    if (comma == std::string::npos) {
      break;
    }
    pos = comma + 1;
  }
  if (fields->empty()) {
    if (error != nullptr) {
      *error = "field list is empty";
    }
    return false;
  }
  return true;
}

bool WritePcd(const std::string& path, const Point* points, size_t count,
              const std::vector<Field>& fields, bool binary, std::string* error) {
  std::ofstream out(path, std::ios::binary | std::ios::trunc);
  if (!out) {
    if (error != nullptr) {
      *error = "failed to open " + path;
    }
    return false;
  }

  // 每个字段查找一次，点循环里只按下标访问，避免逐点线性查表。
  std::vector<const FieldSpec*> columns;
  columns.reserve(fields.size());
  for (Field field : fields) {
    columns.push_back(&SpecOf(field));
  }

  out << "# .PCD v0.7 - Point Cloud Data file format\n";
  out << "VERSION 0.7\n";

  out << "FIELDS";
  for (const FieldSpec* spec : columns) {
    out << ' ' << spec->info.name;
  }
  out << "\nSIZE";
  for (const FieldSpec* spec : columns) {
    out << ' ' << static_cast<unsigned>(spec->info.size);
  }
  out << "\nTYPE";
  for (const FieldSpec* spec : columns) {
    out << ' ' << spec->info.type;
  }
  out << "\nCOUNT";
  for (size_t i = 0; i < columns.size(); ++i) {
    out << " 1";
  }
  out << "\nWIDTH " << count << "\n";
  out << "HEIGHT 1\n";
  out << "VIEWPOINT 0 0 0 1 0 0 0\n";
  out << "POINTS " << count << "\n";
  out << "DATA " << (binary ? "binary" : "ascii") << "\n";

  constexpr size_t kFlushThreshold = 1u << 20;
  std::string buffer;
  buffer.reserve(kFlushThreshold + 256);

  for (size_t i = 0; i < count; ++i) {
    for (size_t j = 0; j < columns.size(); ++j) {
      if (!binary && j != 0) {
        buffer.push_back(' ');
      }
      AppendField(&buffer, points[i], *columns[j], binary);
    }
    if (!binary) {
      buffer.push_back('\n');
    }
    if (buffer.size() >= kFlushThreshold) {
      out.write(buffer.data(), static_cast<std::streamsize>(buffer.size()));
      buffer.clear();
    }
  }

  if (!buffer.empty()) {
    out.write(buffer.data(), static_cast<std::streamsize>(buffer.size()));
  }
  out.flush();
  if (!out) {
    if (error != nullptr) {
      *error = "failed to write " + path;
    }
    return false;
  }
  return true;
}

}  // namespace dense_lidar
