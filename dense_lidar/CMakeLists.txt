
cmake_minimum_required(VERSION 4.4)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

add_executable(test_transform test_transform.cc)

target_include_directories(test_transform PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/../eigen3
)
