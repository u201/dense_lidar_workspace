cmake_minimum_required(VERSION 4.4)
project(dense_lidar LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
    set(CMAKE_BUILD_TYPE Release CACHE STRING "Build type" FORCE)
endif()

# 复用 Hesai SDK 的 pcap/UDP 解析能力；离线转换用不到的可选项全部关闭，
# 这样 Linux 上无需额外安装 OpenSSL，也不会生成 SDK 自带的示例程序。
set(DISENABLE_TEST_CC ON CACHE BOOL "Disable SDK sample executables" FORCE)
set(WITH_PTCS_USE OFF CACHE BOOL "Disable PTC over SSL" FORCE)
add_subdirectory(${CMAKE_CURRENT_SOURCE_DIR}/../HesaiLidar_SDK_2.0
                 ${CMAKE_CURRENT_BINARY_DIR}/HesaiLidar_SDK)

add_executable(dense_lidar
    dense_lidar.cc
    pcd_writer.cc
)

target_include_directories(dense_lidar PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/../cxxopts/include
)

target_link_libraries(dense_lidar PRIVATE
    hesai_sdk_lib
)

# SDK 头文件和本工程源码都含 UTF-8 中文注释，MSVC 需要显式指定编码。
if(MSVC)
    target_compile_options(dense_lidar PRIVATE /utf-8)
endif()

add_executable(test_transform test_transform.cc)

target_include_directories(test_transform PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/../eigen3
)
