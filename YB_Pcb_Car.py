#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import smbus
import math


class YB_Pcb_Car(object):

    def get_i2c_device(self, address, i2c_bus):
        self._addr = address
        if i2c_bus is None:
            return smbus.SMBus(1)
        else:
            return smbus.SMBus(i2c_bus)

    def __init__(self):
        # 🔥 IMPORTANT: using detected address 0x2b
        self._device = self.get_i2c_device(0x2b, 1)

    def write_u8(self, reg, data):
        try:
            self._device.write_byte_data(self._addr, reg, data)
        except:
            print('write_u8 I2C error')

    def write_array(self, reg, data):
        try:
            self._device.write_i2c_block_data(self._addr, reg, data)
        except:
            print('write_array I2C error')

    def Ctrl_Car(self, l_dir, l_speed, r_dir, r_speed):
        try:
            reg = 0x01
            data = [l_dir, l_speed, r_dir, r_speed]
            self.write_array(reg, data)
        except:
            print('Ctrl_Car I2C error')

    def Car_Run(self, speed1, speed2):
        try:
            self.Ctrl_Car(1, speed1, 1, speed2)
        except:
            print('Car_Run I2C error')

    def Car_Back(self, speed1, speed2):
        try:
            self.Ctrl_Car(0, speed1, 0, speed2)
        except:
            print('Car_Back I2C error')

    def Car_Left(self, speed1, speed2):
        try:
            self.Ctrl_Car(0, speed1, 1, speed2)
        except:
            print('Car_Left I2C error')

    def Car_Right(self, speed1, speed2):
        try:
            self.Ctrl_Car(1, speed1, 0, speed2)
        except:
            print('Car_Right I2C error')

    def Car_Stop(self):
        try:
            self.write_u8(0x02, 0x00)
        except:
            print('Car_Stop I2C error')
