"""
Copyright (c) 2018 iCyP
Released under the MIT license
https://opensource.org/licenses/mit-license.php

"""
import json
import os
import struct
from collections import OrderedDict
from math import floor
from sys import float_info
from typing import Any, Dict, List, Optional, Sequence, Tuple

import bmesh
import bpy

from .. import vrm_types
from ..gl_constants import GlConstants
from .glb_bin_collection import GlbBin, GlbBinCollection, ImageBin
from .version import version
from .vrm_helper import find_export_objects, shader_nodes_and_materials


class GlbObj:
    class ValidationError(Exception):
        pass

    def __init__(self, export_invisibles: bool, export_only_selections: bool) -> None:
        if bpy.ops.vrm.model_validate(
            "INVOKE_DEFAULT", show_successful_message=False
        ) != {"FINISHED"}:
            raise self.ValidationError()

        self.export_objects = find_export_objects(
            export_invisibles, export_only_selections
        )
        self.vrm_version: Optional[str] = None
        self.json_dic: Dict[str, Any] = OrderedDict()
        self.bin = b""
        self.glb_bin_collector = GlbBinCollection()
        self.armature = [obj for obj in self.export_objects if obj.type == "ARMATURE"][
            0
        ]
        self.result: Optional[bytes] = None

    def convert_bpy2glb(self, vrm_version: str) -> Optional[bytes]:
        self.vrm_version = vrm_version
        self.image_to_bin()
        self.armature_to_node_and_scenes_dic()
        self.material_to_dic()
        self.mesh_to_bin_and_dic()
        self.json_dic["scene"] = 0
        self.gltf_meta_to_dic()
        self.vrm_meta_to_dic()  # colliderとかmetaとか....
        self.finalize()
        return self.result

    @staticmethod
    def axis_blender_to_glb(vec3: Sequence[float]) -> List[float]:
        return [vec3[i] * t for i, t in zip([0, 2, 1], [-1, 1, 1])]

    @staticmethod
    def textblock2str(textblock: bpy.types.Text) -> str:
        return "".join([line.body for line in textblock.lines])

    def image_to_bin(self) -> None:
        # collect used image
        used_images = []
        used_materials = []
        for mesh in [obj for obj in self.export_objects if obj.type == "MESH"]:
            for mat in mesh.data.materials:
                if mat not in used_materials:
                    used_materials.append(mat)

        # image fetching
        for node, mat in shader_nodes_and_materials(used_materials):
            if node.node_tree["SHADER"] == "MToon_unversioned":
                mat["vrm_shader"] = "MToon_unversioned"
                for (
                    shader_vals
                ) in vrm_types.MaterialMtoon.texture_kind_exchange_dic.values():
                    if shader_vals == "ReceiveShadow_Texture":
                        if node.inputs[shader_vals + "_alpha"].links:
                            n = node.inputs[shader_vals + "_alpha"].links[0].from_node
                            if n.image not in used_images:
                                used_images.append(n.image)
                    elif node.inputs[shader_vals].links:
                        n = node.inputs[shader_vals].links[0].from_node
                        if n.image not in used_images:
                            used_images.append(n.image)
            elif node.node_tree["SHADER"] == "GLTF":
                mat["vrm_shader"] = "GLTF"
                for k in vrm_types.Gltf.TEXTURE_INPUT_NAMES:
                    if node.inputs[k].links:
                        n = node.inputs[k].links[0].from_node
                        if n.image not in used_images:
                            used_images.append(n.image)

            elif node.node_tree["SHADER"] == "TRANSPARENT_ZWRITE":
                mat["vrm_shader"] = "TRANSPARENT_ZWRITE"
                if node.inputs["Main_Texture"].links:
                    n = node.inputs["Main_Texture"].links[0].from_node
                    if n.image not in used_images:
                        used_images.append(n.image)
            else:
                # ?
                pass
        # thumbnail
        if self.armature.get("texture") is not None:
            image = bpy.data.images[self.armature["texture"]]
            if image not in used_images:
                used_images.append(image)

        for image in sorted(
            used_images,
            key=lambda used_image: bpy.data.images.index(used_image)
            if used_image in bpy.data.images.items()
            else len(bpy.data.images) + used_images.index(used_image),
        ):
            with open(image.filepath_from_user(), "rb") as f:
                image_bin = f.read()
            name = image.name
            filetype = "image/" + image.file_format.lower()
            ImageBin(image_bin, name, filetype, self.glb_bin_collector)

    def armature_to_node_and_scenes_dic(self) -> None:
        nodes = []
        scene = []
        skins = []

        bone_id_dic = {
            b.name: bone_id for bone_id, b in enumerate(self.armature.data.bones)
        }

        def bone_to_node(b_bone: bpy.types.Bone) -> Dict[str, Any]:
            parent_head_local = (
                b_bone.parent.head_local if b_bone.parent is not None else [0, 0, 0]
            )
            node = OrderedDict(
                {
                    "name": b_bone.name,
                    "translation": self.axis_blender_to_glb(
                        [b_bone.head_local[i] - parent_head_local[i] for i in range(3)]
                    ),
                    # "rotation":[0,0,0,1],
                    # "scale":[1,1,1],
                    "children": [bone_id_dic[ch.name] for ch in b_bone.children],
                }
            )
            if len(node["children"]) == 0:
                del node["children"]
            return node

        human_bone_node_names = []
        for human_bone in vrm_types.HumanBones.requires + vrm_types.HumanBones.defines:
            if (
                human_bone in self.armature.data.keys()
                and self.armature.data[human_bone]
                and self.armature.data[human_bone]
                in [bone.name for bone in self.armature.data.bones]
            ):
                human_bone_node_names.append(self.armature.data[human_bone])

        for bone in self.armature.data.bones:
            if bone.parent is not None:
                continue

            has_human_bone = False
            if bone.name in human_bone_node_names:
                has_human_bone = True
            skin: Dict[str, Any] = {"joints": []}
            root_bone_id = bone_id_dic[bone.name]
            skin["joints"].append(root_bone_id)
            skin["skeleton"] = root_bone_id
            scene.append(root_bone_id)
            nodes.append(bone_to_node(bone))
            bone_children = list(bone.children)
            while bone_children:
                child = bone_children.pop()
                if child.name in human_bone_node_names:
                    has_human_bone = True
                nodes.append(bone_to_node(child))
                skin["joints"].append(bone_id_dic[child.name])
                bone_children += list(child.children)
            nodes = sorted(nodes, key=lambda node: bone_id_dic[node["name"]])
            if has_human_bone:
                skins.append(skin)

        for skin in skins:
            skin_invert_matrix_bin = b""
            f_4x4_packer = struct.Struct("<16f").pack
            for node_id in skin["joints"]:
                bone_name = nodes[node_id]["name"]
                bone_glb_world_pos = self.axis_blender_to_glb(
                    self.armature.data.bones[bone_name].head_local
                )
                inv_matrix = [
                    1,
                    0,
                    0,
                    0,
                    0,
                    1,
                    0,
                    0,
                    0,
                    0,
                    1,
                    0,
                    -bone_glb_world_pos[0],
                    -bone_glb_world_pos[1],
                    -bone_glb_world_pos[2],
                    1,
                ]
                skin_invert_matrix_bin += f_4x4_packer(*inv_matrix)

            im_bin = GlbBin(
                skin_invert_matrix_bin,
                "MAT4",
                GlConstants.FLOAT,
                len(skin["joints"]),
                None,
                self.glb_bin_collector,
            )
            skin["inverseBindMatrices"] = im_bin.accessor_id

        self.json_dic.update({"scenes": [{"nodes": scene}]})
        self.json_dic.update({"nodes": nodes})
        self.json_dic.update({"skins": skins})

    def material_to_dic(self) -> None:
        glb_material_list = []
        vrm_material_props_list = []

        image_id_dic = {
            image.name: image.image_id for image in self.glb_bin_collector.image_bins
        }
        sampler_dic: Dict[Tuple[int, int], int] = OrderedDict()
        texture_dic: Dict[Tuple[int, int], Any] = OrderedDict()
        sampler_count = 0
        texture_count = 0

        # region texture func

        def add_texture(
            image_name: str, wrap_type: int, filter_type: int
        ) -> Dict[str, Any]:
            nonlocal sampler_count
            nonlocal texture_count
            if (wrap_type, filter_type) not in sampler_dic.keys():
                sampler_dic.update({(wrap_type, filter_type): sampler_count})
                sampler_count += 1
            if (
                image_id_dic[image_name],
                sampler_dic[(wrap_type, filter_type)],
            ) not in texture_dic.keys():
                texture_dic.update(
                    {
                        (
                            image_id_dic[image_name],
                            sampler_dic[(wrap_type, filter_type)],
                        ): texture_count
                    }
                )
                texture_count += 1
            return texture_dic[
                (image_id_dic[image_name], sampler_dic[(wrap_type, filter_type)])
            ]

        def apply_texture_and_sampler_to_dic() -> None:
            if sampler_count > 0:
                sampler_list = self.json_dic["samplers"] = []
                for sampler in sampler_dic.keys():
                    sampler_list.append(
                        {
                            "magFilter": sampler[1],
                            "minFilter": sampler[1],
                            "wrapS": sampler[0],
                            "wrapT": sampler[0],
                        }
                    )
            if texture_count > 0:
                textures = []
                for tex in texture_dic:
                    texture = {"sampler": tex[1], "source": tex[0]}
                    textures.append(texture)
                self.json_dic.update({"textures": textures})

        # region function separate by shader
        def pbr_fallback(
            b_mat: bpy.types.Material,
            base_color: Sequence[float] = (1, 1, 1, 1),
            metalness: float = 0,
            roughness: float = 0.9,
            base_color_texture: Optional[Tuple[str, int, int]] = None,
            metallic_roughness_texture: Optional[Tuple[str, int, int]] = None,
            transparent_method: str = "OPAQUE",
            transparency_cutoff: Optional[float] = 0.5,
            unlit: bool = True,
            doublesided: bool = False,
        ) -> Dict[str, Any]:
            """transparent_method = {"OPAQUE","MASK","BLEND"}"""
            fallback_dic = {
                "name": b_mat.name,
                "pbrMetallicRoughness": {
                    "baseColorFactor": base_color,
                    "metallicFactor": metalness,
                    "roughnessFactor": roughness,
                },
            }
            for k, v in fallback_dic["pbrMetallicRoughness"].items():
                if v is None:
                    del fallback_dic["pbrMetallicRoughness"][k]

            if base_color_texture is not None:
                fallback_dic["pbrMetallicRoughness"].update(
                    {
                        "baseColorTexture": {
                            "index": add_texture(*base_color_texture),
                            "texCoord": 0,
                        }
                    }  # TODO:
                )
            if metallic_roughness_texture is not None:
                fallback_dic["pbrMetallicRoughness"].update(
                    {
                        "metallicRoughnessTexture": {
                            "index": add_texture(*metallic_roughness_texture),
                            "texCoord": 0,  # TODO:
                        }
                    }
                )
            fallback_dic["alphaMode"] = transparent_method
            if transparent_method == "MASK":
                fallback_dic["alphaCutoff"] = (
                    0.5 if transparency_cutoff is None else transparency_cutoff
                )
            if unlit:
                fallback_dic["extensions"] = {"KHR_materials_unlit": {}}
            fallback_dic["doubleSided"] = doublesided
            return fallback_dic

        # region util func
        def get_texture_name_and_sampler_type(
            shader_node: bpy.types.ShaderNode, input_socket_name: str
        ) -> Optional[Tuple[str, int, int]]:
            if (
                not shader_node.inputs.get(input_socket_name)
                or not shader_node.inputs.get(input_socket_name).links
            ):
                return None

            tex_name = (
                shader_node.inputs.get(input_socket_name).links[0].from_node.image.name
            )
            # blender is ('Linear', 'Closest', 'Cubic', 'Smart') glTF is Linear, Closest
            if (
                shader_node.inputs.get(input_socket_name)
                .links[0]
                .from_node.interpolation
                == "Closest"
            ):
                filter_type = GlConstants.NEAREST
            else:
                filter_type = GlConstants.LINEAR
            # blender is ('REPEAT', 'EXTEND', 'CLIP') glTF is CLAMP_TO_EDGE,MIRRORED_REPEAT,REPEAT
            if (
                shader_node.inputs.get(input_socket_name).links[0].from_node.extension
                == "REPEAT"
            ):
                wrap_type = GlConstants.REPEAT
            else:
                wrap_type = GlConstants.CLAMP_TO_EDGE
            return tex_name, wrap_type, filter_type

        def get_float_value(
            shader_node: bpy.types.ShaderNode, input_socket_name: str
        ) -> Optional[float]:
            float_val = None
            if shader_node.inputs.get(input_socket_name):
                if shader_node.inputs.get(input_socket_name).links:
                    float_val = (
                        shader_node.inputs.get(input_socket_name)
                        .links[0]
                        .from_node.outputs[0]
                        .default_value
                    )
                else:
                    float_val = shader_node.inputs.get(input_socket_name).default_value
            return float_val

        def get_rgba_val(
            shader_node: bpy.types.ShaderNode, input_socket_name: str
        ) -> Optional[List[float]]:
            rgba_val = None
            if shader_node.inputs.get(input_socket_name):
                if shader_node.inputs.get(input_socket_name).links:
                    rgba_val = [
                        shader_node.inputs.get(input_socket_name)
                        .links[0]
                        .from_node.outputs[0]
                        .default_value[i]
                        for i in range(4)
                    ]
                else:
                    rgba_val = [
                        shader_node.inputs.get(input_socket_name).default_value[i]
                        for i in range(4)
                    ]
            return rgba_val

        # endregion util func

        def make_mtoon_unversioned_extension_dic(
            b_mat: bpy.types.Material, mtoon_shader_node: bpy.types.ShaderNode
        ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
            mtoon_dic: Dict[str, Any] = OrderedDict()
            mtoon_dic["name"] = b_mat.name
            mtoon_dic["shader"] = "VRM/MToon"
            mtoon_dic["keywordMap"] = {}
            keyword_map = mtoon_dic["keywordMap"]
            mtoon_dic["tagMap"] = {}
            tag_map = mtoon_dic["tagMap"]
            mtoon_dic["floatProperties"] = OrderedDict()
            mtoon_float_dic: Dict[str, float] = mtoon_dic["floatProperties"]
            mtoon_dic["vectorProperties"] = OrderedDict()
            mtoon_vector_dic: Dict[str, List[float]] = mtoon_dic["vectorProperties"]
            mtoon_dic["textureProperties"] = OrderedDict()
            mtoon_texture_dic = mtoon_dic["textureProperties"]

            outline_width_mode = 0
            outline_color_mode = 0
            for float_key, float_prop in [
                (k, val)
                for k, val in vrm_types.MaterialMtoon.float_props_exchange_dic.items()
                if val is not None
            ]:
                float_val = get_float_value(mtoon_shader_node, float_prop)
                if float_val is not None:
                    mtoon_float_dic[float_key] = float_val
                    if float_key == "_OutlineWidthMode":
                        outline_width_mode = min(max(round(float_val), 0), 2)
                        mtoon_float_dic[float_key] = int(outline_width_mode)
                    if float_key == "_OutlineColorMode":
                        outline_color_mode = min(max(round(float_val), 0), 1)
                        mtoon_float_dic[float_key] = int(outline_color_mode)

            def outline_keyword_set(
                width_world: bool,
                width_screen: bool,
                color_fixed: bool,
                color_mixed: bool,
            ) -> None:
                if width_world:
                    keyword_map["MTOON_OUTLINE_WIDTH_WORLD"] = width_world
                elif width_screen:
                    keyword_map["MTOON_OUTLINE_WIDTH_SCREEN"] = width_screen
                if color_fixed:
                    keyword_map["MTOON_OUTLINE_COLOR_FIXED"] = color_fixed
                elif color_mixed:
                    keyword_map["MTOON_OUTLINE_COLOR_MIXED"] = color_mixed

            if outline_width_mode < 1:
                outline_keyword_set(False, False, False, False)
            elif outline_width_mode < 2:
                if outline_color_mode < 1:
                    outline_keyword_set(True, False, True, False)
                else:
                    outline_keyword_set(True, False, False, True)

            elif outline_width_mode >= 2:
                if outline_color_mode < 1:
                    outline_keyword_set(False, True, True, False)
                else:
                    outline_keyword_set(False, True, False, True)

            vec_props = list(
                dict.fromkeys(
                    vrm_types.MaterialMtoon.vector_props_exchange_dic.values()
                )
            )
            for (
                remove_vec_prop
            ) in vrm_types.MaterialMtoon.texture_kind_exchange_dic.values():
                if remove_vec_prop in vec_props:
                    vec_props.remove(remove_vec_prop)

            for vector_key, vector_prop in [
                (k, v)
                for k, v in vrm_types.MaterialMtoon.vector_props_exchange_dic.items()
                if v in vec_props
            ]:
                vector_val = get_rgba_val(mtoon_shader_node, vector_prop)
                if vector_val is not None:
                    mtoon_vector_dic[vector_key] = vector_val

            use_normalmap = False
            maintex: Optional[Tuple[str, int, int]] = None
            for (
                texture_key,
                texture_prop,
            ) in vrm_types.MaterialMtoon.texture_kind_exchange_dic.items():
                tex = get_texture_name_and_sampler_type(mtoon_shader_node, texture_prop)
                if tex is None:
                    continue

                mtoon_texture_dic[texture_key] = add_texture(*tex)
                mtoon_vector_dic[texture_key] = [0, 0, 1, 1]
                if texture_prop == "MainTexture":
                    maintex = tex
                    uv_offset_scaling_node = None
                    try:
                        uv_offset_scaling_node = (
                            mtoon_shader_node.inputs[texture_prop]
                            .links[0]
                            .from_node.inputs[0]
                            .links[0]
                            .from_node
                        )
                    except IndexError:
                        uv_offset_scaling_node = None
                    if (
                        uv_offset_scaling_node is not None
                        and uv_offset_scaling_node.type == "MAPPING'"
                    ):
                        if bpy.app.version[1] == 80:
                            mtoon_vector_dic[texture_key] = [
                                uv_offset_scaling_node.translation[0],
                                uv_offset_scaling_node.translation[1],
                                uv_offset_scaling_node.scale[0],
                                uv_offset_scaling_node.scale[1],
                            ]
                        else:
                            mtoon_vector_dic[texture_key] = [
                                uv_offset_scaling_node.inputs["Location"].default_value[
                                    0
                                ],
                                uv_offset_scaling_node.inputs["Location"].default_value[
                                    1
                                ],
                                uv_offset_scaling_node.inputs["Scale"].default_value[0],
                                uv_offset_scaling_node.inputs["Scale"].default_value[1],
                            ]
                    else:
                        mtoon_vector_dic[texture_key] = [0, 0, 1, 1]
                elif texture_prop == "NomalmapTexture":
                    use_normalmap = True

            def material_prop_setter(
                blend_mode: int,
                src_blend: int,
                dst_blend: int,
                z_write: int,
                alphatest: bool,
                render_queue: int,
                render_type: str,
            ) -> None:
                mtoon_float_dic["_BlendMode"] = blend_mode
                mtoon_float_dic["_SrcBlend"] = src_blend
                mtoon_float_dic["_DstBlend"] = dst_blend
                mtoon_float_dic["_ZWrite"] = z_write
                if alphatest:
                    keyword_map.update({"_ALPHATEST_ON": alphatest})
                mtoon_dic["renderQueue"] = render_queue
                tag_map["RenderType"] = render_type

            if b_mat.blend_method == "OPAQUE":
                material_prop_setter(0, 1, 0, 1, False, -1, "Opaque")
            elif b_mat.blend_method == "CLIP":
                material_prop_setter(1, 1, 0, 1, True, 2450, "TransparentCutout")
                mtoon_float_dic["_Cutoff"] = b_mat.alpha_threshold
            else:  # transparent and Z_TRANSPARENCY or Raytrace
                material_prop_setter(2, 5, 10, 0, False, 3000, "Transparent")
            keyword_map.update(
                {"_ALPHABLEND_ON": b_mat.blend_method not in ("OPAQUE", "CLIP")}
            )
            keyword_map.update({"_ALPHAPREMULTIPLY_ON": False})

            mtoon_float_dic["_MToonVersion"] = vrm_types.MaterialMtoon.version
            mtoon_float_dic["_CullMode"] = (
                2 if b_mat.use_backface_culling else 0
            )  # no cull or bf cull
            mtoon_float_dic[
                "_OutlineCullMode"
            ] = 1  # front face cull (for invert normal outline)
            mtoon_float_dic["_DebugMode"] = 0
            keyword_map.update({"MTOON_DEBUG_NORMAL": False})
            keyword_map.update({"MTOON_DEBUG_LITSHADERATE": False})
            if use_normalmap:
                keyword_map.update({"_NORMALMAP": use_normalmap})

            # for pbr_fallback
            if b_mat.blend_method == "OPAQUE":
                transparent_method = "OPAQUE"
                transparency_cutoff = None
            elif b_mat.blend_method == "CLIP":
                transparent_method = "MASK"
                transparency_cutoff = b_mat.alpha_threshold
            else:
                transparent_method = "BLEND"
                transparency_cutoff = None
            pbr_dic = pbr_fallback(
                b_mat,
                base_color=mtoon_vector_dic["_Color"],
                base_color_texture=maintex,
                transparent_method=transparent_method,
                transparency_cutoff=transparency_cutoff,
                doublesided=not b_mat.use_backface_culling,
            )
            vrm_version = self.vrm_version
            if vrm_version is None:
                raise Exception("vrm version is None")
            if vrm_version.startswith("1."):
                mtoon_ext_dic: Dict[str, Any] = {}
                mtoon_ext_dic["properties"] = {}
                mt_prop = mtoon_ext_dic["properties"]
                mt_prop["version"] = "3.2"
                blendmode = mtoon_float_dic.get("_BlendMode")
                if blendmode == 0:
                    blendmode_str = "opaque"
                elif blendmode == 1:
                    blendmode_str = "cutout"
                else:
                    blendmode_str = "transparent"
                # TODO transparentWithZWrite
                mt_prop["renderMode"] = blendmode_str

                mt_prop["cullMode"] = (
                    mtoon_float_dic.get("_CullMode") == "back"
                    if b_mat.use_backface_culling
                    else "off"
                )  # no cull or bf cull
                # TODO unknown number
                mt_prop["renderQueueOffsetNumber"] = 0

                mt_prop["litFactor"] = mtoon_vector_dic.get("_Color")
                mt_prop["litMultiplyTexture"] = mtoon_texture_dic.get("_MainTex")
                mt_prop["shadeFactor"] = mtoon_vector_dic.get("_ShadeColor")
                mt_prop["shadeMultiplyTexture"] = mtoon_texture_dic.get("_ShadeTexture")
                mt_prop["cutoutThresholdFactor"] = mtoon_float_dic.get("_Cutoff")
                mt_prop["shadingShiftFactor"] = mtoon_float_dic.get("_ShadeShift")
                mt_prop["shadingToonyFactor"] = mtoon_float_dic.get("_ShadeToony")
                mt_prop["shadowReceiveMultiplierFactor"] = mtoon_float_dic.get(
                    "_ReceiveShadowRate"
                )
                mt_prop[
                    "shadowReceiveMultiplierMultiplyTexture"
                ] = mtoon_texture_dic.get("_ReceiveShadowTexture")
                mt_prop["litAndShadeMixingMultiplierFactor"] = mtoon_float_dic.get(
                    "_ShadingGradeRate"
                )
                mt_prop[
                    "litAndShadeMixingMultiplierMultiplyTexture"
                ] = mtoon_texture_dic.get("_ShadingGradeTexture")
                mt_prop["lightColorAttenuationFactor"] = mtoon_float_dic.get(
                    "_LightColorAttenuation"
                )
                mt_prop["giIntensityFactor"] = mtoon_float_dic.get(
                    "_IndirectLightIntensity"
                )
                mt_prop["normalTexture"] = mtoon_texture_dic.get("_BumpMap")
                mt_prop["normalScaleFactor"] = mtoon_float_dic.get("_BumpScale")
                mt_prop["emissionFactor"] = mtoon_vector_dic.get("_EmissionColor")
                mt_prop["emissionMultiplyTexture"] = mtoon_texture_dic.get(
                    "_EmissionMap"
                )
                mt_prop["additiveTexture"] = mtoon_texture_dic.get("_SphereAdd")
                mt_prop["rimFactor"] = mtoon_vector_dic.get("_RimColor")
                mt_prop["rimMultiplyTexture"] = mtoon_texture_dic.get("_RimTexture")
                mt_prop["rimLightingMixFactor"] = mtoon_float_dic.get("_RimLightingMix")
                mt_prop["rimFresnelPowerFactor"] = mtoon_float_dic.get(
                    "_RimFresnelPower"
                )
                mt_prop["rimLiftFactor"] = mtoon_float_dic.get("_RimLift")
                mt_prop["outlineWidthMode"] = [
                    "none",
                    "worldCoordinates",
                    "screenCoordinates",
                ][floor(mtoon_float_dic.get("_OutlineWidthMode", 0))]
                mt_prop["outlineWidthFactor"] = mtoon_vector_dic.get("_OutlineColor")
                mt_prop["outlineWidthMultiplyTexture"] = mtoon_texture_dic.get(
                    "_OutlineWidthTexture"
                )
                mt_prop["outlineScaledMaxDistanceFactor"] = mtoon_float_dic.get(
                    "_OutlineScaledMaxDistance"
                )
                mt_prop["outlineColorMode"] = ["fixedColor", "mixedLighting"][
                    floor(mtoon_float_dic.get("_OutlineLightingMix", 0))
                ]
                mt_prop["outlineFactor"] = mtoon_float_dic.get("_OutlineWidth")
                mt_prop["outlineLightingMixFactor"] = mtoon_float_dic.get(
                    "OutlineLightingMix"
                )

                uv_transforms = mtoon_vector_dic.get("_MainTex")
                if uv_transforms is None:
                    uv_transforms = [0, 0, 1, 1]
                mt_prop["mainTextureLeftBottomOriginOffset"] = uv_transforms[0:2]
                mt_prop["mainTextureLeftBottomOriginScale"] = uv_transforms[2:4]
                mt_prop["uvAnimationMaskTexture"] = mtoon_texture_dic.get(
                    "_UvAnimMaskTexture"
                )
                mt_prop["uvAnimationScrollXSpeedFactor"] = mtoon_float_dic.get(
                    "_UvAnimScrollX"
                )
                mt_prop["uvAnimationScrollYSpeedFactor"] = mtoon_float_dic.get(
                    "_UvAnimScrollY"
                )
                mt_prop["uvAnimationRotationSpeedFactor"] = mtoon_float_dic.get(
                    "_UvAnimRotation"
                )
                garbage_list = []
                for k, v in mt_prop.items():
                    if v is None:
                        garbage_list.append(k)
                for garbage in garbage_list:
                    mt_prop.pop(garbage)

                pbr_dic["extensions"].update({"VRMC_materials_mtoon": mtoon_ext_dic})
            return mtoon_dic, pbr_dic

        def make_gltf_mat_dic(
            b_mat: bpy.types.Material, gltf_shader_node: bpy.types.ShaderNode
        ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
            gltf_dic = OrderedDict()
            gltf_dic["name"] = b_mat.name
            gltf_dic["shader"] = "VRM_USE_GLTFSHADER"
            gltf_dic["keywordMap"] = {}
            gltf_dic["tagMap"] = {}
            gltf_dic["floatProperties"] = {}
            gltf_dic["vectorProperties"] = {}
            gltf_dic["textureProperties"] = {}

            if b_mat.blend_method == "OPAQUE":
                transparent_method = "OPAQUE"
                transparency_cutoff = None
            elif b_mat.blend_method == "CLIP":
                transparent_method = "MASK"
                transparency_cutoff = b_mat.alpha_threshold
            else:
                transparent_method = "BLEND"
                transparency_cutoff = None

            pbr_dic = pbr_fallback(
                b_mat,
                base_color=get_rgba_val(gltf_shader_node, "base_Color"),
                metalness=get_float_value(gltf_shader_node, "metallic"),
                roughness=get_float_value(gltf_shader_node, "roughness"),
                base_color_texture=get_texture_name_and_sampler_type(
                    gltf_shader_node, "color_texture"
                ),
                metallic_roughness_texture=get_texture_name_and_sampler_type(
                    gltf_shader_node, "metallic_roughness_texture"
                ),
                transparent_method=transparent_method,
                transparency_cutoff=transparency_cutoff,
                unlit=get_float_value(gltf_shader_node, "unlit") >= 0.5,
                doublesided=not b_mat.use_backface_culling,
            )

            def pbr_tex_add(texture_type: str, socket_name: str) -> None:
                img = get_texture_name_and_sampler_type(gltf_shader_node, socket_name)
                if img is not None:
                    pbr_dic[texture_type] = {"index": add_texture(*img), "texCoord": 0}
                else:
                    print(socket_name)

            pbr_tex_add("normalTexture", "normal")
            pbr_tex_add("emissiveTexture", "emissive_texture")
            pbr_tex_add("occlusionTexture", "occlusion_texture")
            pbr_dic["emissiveFactor"] = get_rgba_val(
                gltf_shader_node, "emissive_color"
            )[0:3]

            return gltf_dic, pbr_dic

        def make_transzw_mat_dic(
            b_mat: bpy.types.Material, transzw_shader_node: bpy.types.ShaderNode
        ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
            zw_dic = OrderedDict()
            zw_dic["name"] = b_mat.name
            zw_dic["shader"] = "VRM/UnlitTransparentZWrite"
            zw_dic["renderQueue"] = 2600
            zw_dic["keywordMap"] = {}
            zw_dic["tagMap"] = {"RenderType": "Transparent"}
            zw_dic["floatProperties"] = {}
            zw_dic["vectorProperties"] = {}
            zw_dic["textureProperties"] = {}
            color_tex = get_texture_name_and_sampler_type(
                transzw_shader_node, "Main_Texture"
            )
            if color_tex is not None:
                zw_dic["textureProperties"] = {"_MainTex": add_texture(*color_tex)}
                zw_dic["vectorProperties"] = {"_MainTex": [0, 0, 1, 1]}
            pbr_dic = pbr_fallback(
                b_mat, base_color_texture=color_tex, transparent_method="BLEND"
            )

            return zw_dic, pbr_dic

        # endregion function separate by shader

        used_materials = []
        for mesh in [obj for obj in self.export_objects if obj.type == "MESH"]:
            for mat in mesh.data.materials:
                if mat not in used_materials:
                    used_materials.append(mat)

        for b_mat in used_materials:
            if b_mat["vrm_shader"] == "MToon_unversioned":
                for node in b_mat.node_tree.nodes:
                    if node.type == "OUTPUT_MATERIAL":
                        mtoon_shader_node = node.inputs["Surface"].links[0].from_node
                        break
                material_properties_dic, pbr_dic = make_mtoon_unversioned_extension_dic(
                    b_mat, mtoon_shader_node
                )
            elif b_mat["vrm_shader"] == "GLTF":
                for node in b_mat.node_tree.nodes:
                    if node.type == "OUTPUT_MATERIAL":
                        gltf_shader_node = node.inputs["Surface"].links[0].from_node
                        break
                material_properties_dic, pbr_dic = make_gltf_mat_dic(
                    b_mat, gltf_shader_node
                )
            elif b_mat["vrm_shader"] == "TRANSPARENT_ZWRITE":
                for node in b_mat.node_tree.nodes:
                    if node.type == "OUTPUT_MATERIAL":
                        zw_shader_node = node.inputs["Surface"].links[0].from_node
                        break
                material_properties_dic, pbr_dic = make_transzw_mat_dic(
                    b_mat, zw_shader_node
                )
            else:
                message = "VRM doesn't support \"" + b_mat["vrm_shader"] + '" shader.'
                print(message)
                raise Exception(message)

            glb_material_list.append(pbr_dic)
            vrm_material_props_list.append(material_properties_dic)

        apply_texture_and_sampler_to_dic()
        self.json_dic.update({"materials": glb_material_list})
        vrm_version = self.vrm_version
        if vrm_version is None:
            raise Exception("vrm version is None")
        if vrm_version.startswith("0."):
            self.json_dic.update(
                {"extensions": {"VRM": {"materialProperties": vrm_material_props_list}}}
            )

    def joint_id_from_node_name_solver(
        self, node_name: str, node_id_dic: Dict[str, int]
    ) -> int:
        try:
            node_id = node_id_dic[node_name]
            return self.json_dic["skins"][0]["joints"].index(node_id)
        except (ValueError, KeyError):
            print(f"{node_name} bone may be not exist")
            return -1  # 存在しないボーンを指してる場合は-1を返す

    @staticmethod
    def fetch_morph_vertex_normal_difference(
        mesh_data: bpy.types.Mesh,
    ) -> Dict[str, List[List[float]]]:
        morph_normal_diff_dic = {}
        vert_base_normal_dic = OrderedDict()
        for kb in mesh_data.shape_keys.key_blocks:
            vert_base_normal_dic.update({kb.name: kb.normals_vertex_get()})
        reference_key_name = mesh_data.shape_keys.reference_key.name
        for k, v in vert_base_normal_dic.items():
            if k == reference_key_name:
                continue
            values = []
            for vert_morph_normal, vert_base_normal in zip(
                zip(*[iter(v)] * 3),
                zip(*[iter(vert_base_normal_dic[reference_key_name])] * 3),
            ):
                values.append(
                    [vert_morph_normal[i] - vert_base_normal[i] for i in range(3)]
                )
            morph_normal_diff_dic.update({k: values})
        return morph_normal_diff_dic

    def mesh_to_bin_and_dic(self) -> None:
        self.json_dic["meshes"] = []
        vrm_version = self.vrm_version
        if vrm_version is None:
            raise Exception("vrm version is None")
        for mesh_id, mesh in enumerate(
            [obj for obj in self.export_objects if obj.type == "MESH"]
        ):
            is_skin_mesh = True
            if (
                len([m for m in mesh.modifiers if m.type == "ARMATURE"]) == 0
                and mesh.parent is not None
                and mesh.parent.type == "ARMATURE"
                and mesh.parent_bone is not None
            ):
                is_skin_mesh = False
            node_dic = OrderedDict(
                {
                    "name": mesh.name,
                    "translation": self.axis_blender_to_glb(mesh.location),
                    "rotation": [0, 0, 0, 1],  # このへんは規約なので
                    "scale": [1, 1, 1],  # このへんは規約なので
                    "mesh": mesh_id,
                }
            )
            if is_skin_mesh:
                node_dic["translation"] = [0, 0, 0]  # skinnedmeshはtransformを無視される
                # TODO: 決め打ちってどうよ:一体のモデルなのだから2つもあっては困る(から決め打ち(やめろ(やだ))
                node_dic["skin"] = 0
            self.json_dic["nodes"].append(node_dic)

            mesh_node_id = len(self.json_dic["nodes"]) - 1

            if is_skin_mesh:
                self.json_dic["scenes"][0]["nodes"].append(mesh_node_id)
            else:
                parent_node = (
                    [
                        node
                        for node in self.json_dic["nodes"]
                        if node["name"] == mesh.parent_bone
                    ]
                    + [None]
                )[0]
                base_pos = [0, 0, 0]
                if parent_node:
                    if "children" in parent_node.keys():
                        parent_node["children"].append(mesh_node_id)
                    else:
                        parent_node["children"] = [mesh_node_id]
                    base_pos = self.armature.data.bones[mesh.parent_bone].head_local
                else:
                    self.json_dic["scenes"][0]["nodes"].append(mesh_node_id)
                relate_pos = [mesh.location[i] - base_pos[i] for i in range(3)]
                self.json_dic["nodes"][mesh_node_id][
                    "translation"
                ] = self.axis_blender_to_glb(relate_pos)

            # region hell
            bpy.ops.object.mode_set(mode="OBJECT")

            # region glTF-Blender-IO
            # https://github.com/KhronosGroup/glTF-Blender-IO/blob/blender-v2.91-release/addons/io_scene_gltf2/blender/exp/gltf2_blender_gather_nodes.py#L285-L303
            # http://www.apache.org/licenses/LICENSE-2.0
            armature_modifiers = {}
            if is_skin_mesh:
                # temporarily disable Armature modifiers if exporting skins
                for idx, modifier in enumerate(mesh.modifiers):
                    if modifier.type == "ARMATURE":
                        armature_modifiers[idx] = modifier.show_viewport
                        modifier.show_viewport = False

            depsgraph = bpy.context.evaluated_depsgraph_get()
            mesh_owner = mesh.evaluated_get(depsgraph)
            mesh_data = mesh_owner.to_mesh(
                preserve_all_data_layers=True, depsgraph=depsgraph
            ).copy()
            for prop in mesh.data.keys():
                mesh_data[prop] = mesh.data[prop]

            if is_skin_mesh:
                # restore Armature modifiers
                for idx, show_viewport in armature_modifiers.items():
                    mesh.modifiers[idx].show_viewport = show_viewport
            # endregion glTF-Blender-IO

            mesh.hide_viewport = False
            mesh.hide_select = False
            bpy.context.view_layer.objects.active = mesh
            bpy.ops.object.mode_set(mode="EDIT")

            bm_temp = bmesh.new()
            bm_temp.from_mesh(mesh_data)
            bmesh.ops.transform(bm_temp, matrix=mesh.matrix_world, verts=bm_temp.verts)
            if not is_skin_mesh:
                # TODO:
                bmesh.ops.translate(bm_temp, vec=-mesh.location)
            bmesh.ops.triangulate(bm_temp, faces=bm_temp.faces[:])
            bm_temp.to_mesh(mesh_data)
            bm_temp.free()

            if mesh_data.has_custom_normals:
                mesh_data.calc_loop_triangles()
                mesh_data.calc_normals_split()

            bm = bmesh.new()
            bm.from_mesh(mesh_data)

            # region temporary used
            mat_id_dic = {
                mat["name"]: i for i, mat in enumerate(self.json_dic["materials"])
            }
            material_slot_dic = {
                i: mat.name for i, mat in enumerate(mesh.material_slots)
            }
            node_id_dic = {
                node["name"]: i for i, node in enumerate(self.json_dic["nodes"])
            }

            v_group_name_dic = {i: vg.name for i, vg in enumerate(mesh.vertex_groups)}
            fmin, fmax = -float_info.max, float_info.max  # .minはfloatで一番細かい正の数を示す。
            unique_vertex_id = 0
            # {(uv...,vertex_index):unique_vertex_id} (uvと頂点番号が同じ頂点は同じものとして省くようにする)
            unique_vertex_dic = {}
            uvlayers_dic = {
                i: uvlayer.name for i, uvlayer in enumerate(mesh_data.uv_layers)
            }

            # endregion  temporary_used
            primitive_index_bin_dic = OrderedDict(
                {mat_id_dic[mat.name]: b"" for mat in mesh.material_slots}
            )
            primitive_index_vertex_count = OrderedDict(
                {mat_id_dic[mat.name]: 0 for mat in mesh.material_slots}
            )
            if mesh_data.shape_keys is None:
                shape_pos_bin_dic = {}
                shape_normal_bin_dic = {}
                shape_min_max_dic = {}
                morph_normal_diff_dic = {}
            else:
                # 0番目Basisは省く
                shape_pos_bin_dic = OrderedDict(
                    {shape.name: b"" for shape in mesh_data.shape_keys.key_blocks[1:]}
                )
                shape_normal_bin_dic = OrderedDict(
                    {shape.name: b"" for shape in mesh_data.shape_keys.key_blocks[1:]}
                )
                shape_min_max_dic = OrderedDict(
                    {
                        shape.name: [[fmax, fmax, fmax], [fmin, fmin, fmin]]
                        for shape in mesh_data.shape_keys.key_blocks[1:]
                    }
                )
                morph_normal_diff_dic = (
                    self.fetch_morph_vertex_normal_difference(mesh_data)
                    if vrm_version.startswith("0.")
                    else {}
                )  # {morphname:{vertexid:[diff_X,diff_y,diff_z]}}
            position_bin = b""
            position_min_max = [[fmax, fmax, fmax], [fmin, fmin, fmin]]
            normal_bin = b""
            joints_bin = b""
            weights_bin = b""
            texcoord_bins = {uvlayer_id: b"" for uvlayer_id in uvlayers_dic.keys()}
            float_vec4_packer = struct.Struct("<ffff").pack
            float_vec3_packer = struct.Struct("<fff").pack
            float_pair_packer = struct.Struct("<ff").pack
            unsigned_int_scalar_packer = struct.Struct("<I").pack
            unsigned_short_vec4_packer = struct.Struct("<HHHH").pack

            def min_max(minmax: List[List[float]], position: List[float]) -> None:
                for i in range(3):
                    minmax[0][i] = (
                        position[i] if position[i] < minmax[0][i] else minmax[0][i]
                    )
                    minmax[1][i] = (
                        position[i] if position[i] > minmax[1][i] else minmax[1][i]
                    )

            for face in bm.faces:
                for loop in face.loops:
                    uv_list = []
                    for uvlayer_name in uvlayers_dic.values():
                        uv_layer = bm.loops.layers.uv[uvlayer_name]
                        uv_list += [loop[uv_layer].uv[0], loop[uv_layer].uv[1]]

                    vert_normal = [0, 0, 0]
                    if mesh_data.has_custom_normals:
                        tri = mesh_data.loop_triangles[face.index]
                        vid = -1
                        for i, _vid in enumerate(tri.vertices):
                            if _vid == loop.vert.index:
                                vid = i
                        if vid == -1:
                            print("something wrong in custom normal export")
                        vert_normal = tri.split_normals[vid]
                    else:
                        if face.smooth:
                            vert_normal = loop.vert.normal
                        else:
                            vert_normal = face.normal

                    vertex_key = (*uv_list, *vert_normal, loop.vert.index)
                    cached_vert_id = unique_vertex_dic.get(
                        vertex_key
                    )  # keyがなければNoneを返す
                    if cached_vert_id is not None:
                        primitive_index_bin_dic[
                            mat_id_dic[material_slot_dic[face.material_index]]
                        ] += unsigned_int_scalar_packer(cached_vert_id)
                        primitive_index_vertex_count[
                            mat_id_dic[material_slot_dic[face.material_index]]
                        ] += 1
                        continue
                    unique_vertex_dic[vertex_key] = unique_vertex_id
                    for uvlayer_id, uvlayer_name in uvlayers_dic.items():
                        uv_layer = bm.loops.layers.uv[uvlayer_name]
                        uv = loop[uv_layer].uv
                        texcoord_bins[uvlayer_id] += float_pair_packer(
                            uv[0], 1 - uv[1]
                        )  # blenderとglbのuvは上下逆
                    for shape_name in shape_pos_bin_dic:
                        shape_layer = bm.verts.layers.shape[shape_name]
                        morph_pos = self.axis_blender_to_glb(
                            [
                                loop.vert[shape_layer][i] - loop.vert.co[i]
                                for i in range(3)
                            ]
                        )
                        shape_pos_bin_dic[shape_name] += float_vec3_packer(*morph_pos)
                        if vrm_version.startswith("0."):
                            shape_normal_bin_dic[shape_name] += float_vec3_packer(
                                *self.axis_blender_to_glb(
                                    morph_normal_diff_dic[shape_name][loop.vert.index]
                                )
                            )
                        min_max(shape_min_max_dic[shape_name], morph_pos)
                    if is_skin_mesh:
                        weight_and_joint_list: List[Tuple[float, int]] = []
                        for v_group in mesh_data.vertices[loop.vert.index].groups:
                            joint_id = self.joint_id_from_node_name_solver(
                                v_group_name_dic[v_group.group], node_id_dic
                            )
                            # 存在しないボーンを指してる場合は-1を返されてるので、その場合は飛ばす
                            if joint_id == -1:
                                continue
                            # ウエイトがゼロのジョイントの値は無視してゼロになるようにする
                            # https://github.com/KhronosGroup/glTF/tree/f33f90ad9439a228bf90cde8319d851a52a3f470/specification/2.0#skinned-mesh-attributes
                            if v_group.weight < float_info.epsilon:
                                continue

                            weight_and_joint_list.append((v_group.weight, joint_id))

                        while len(weight_and_joint_list) < 4:
                            weight_and_joint_list.append((0.0, 0))

                        weight_and_joint_list.sort(reverse=True)

                        if len(weight_and_joint_list) > 4:
                            print(
                                f"Joints on vertex id:{loop.vert.index} in: {mesh.name} are truncated"
                            )
                            weight_and_joint_list = weight_and_joint_list[:4]

                        weights = [weight for weight, _ in weight_and_joint_list]
                        joints = [joint for _, joint in weight_and_joint_list]

                        if sum(weights) < float_info.epsilon:
                            print(
                                f"No weight on vertex id:{loop.vert.index} in: {mesh.name}"
                            )

                            # Attach hips bone
                            hips_bone_name = self.armature.data["hips"]
                            hips_bone_index = next(
                                index
                                for index, node in enumerate(self.json_dic["nodes"])
                                if node["name"] == hips_bone_name
                            )
                            weights = [1.0, 0, 0, 0]
                            joints = [hips_bone_index, 0, 0, 0]

                        normalized_weights = (
                            vrm_types.normalize_weights_compatible_with_gl_float(
                                weights
                            )
                        )
                        joints_bin += unsigned_short_vec4_packer(*joints)
                        weights_bin += float_vec4_packer(*normalized_weights)

                    vert_location = self.axis_blender_to_glb(loop.vert.co)
                    position_bin += float_vec3_packer(*vert_location)
                    min_max(position_min_max, vert_location)
                    normal_bin += float_vec3_packer(
                        *self.axis_blender_to_glb(vert_normal)
                    )
                    primitive_index_bin_dic[
                        mat_id_dic[material_slot_dic[face.material_index]]
                    ] += unsigned_int_scalar_packer(unique_vertex_id)
                    primitive_index_vertex_count[
                        mat_id_dic[material_slot_dic[face.material_index]]
                    ] += 1
                    unique_vertex_id += 1  # noqa: SIM113

            # DONE :index position, uv, normal, position morph,JOINT WEIGHT
            # TODO: morph_normal, v_color...?
            primitive_glbs_dic = OrderedDict(
                {
                    mat_id: GlbBin(
                        index_bin,
                        "SCALAR",
                        GlConstants.UNSIGNED_INT,
                        primitive_index_vertex_count[mat_id],
                        None,
                        self.glb_bin_collector,
                    )
                    for mat_id, index_bin in primitive_index_bin_dic.items()
                    if index_bin != b""
                }
            )
            pos_glb = GlbBin(
                position_bin,
                "VEC3",
                GlConstants.FLOAT,
                unique_vertex_id,
                position_min_max,
                self.glb_bin_collector,
            )
            nor_glb = GlbBin(
                normal_bin,
                "VEC3",
                GlConstants.FLOAT,
                unique_vertex_id,
                None,
                self.glb_bin_collector,
            )
            uv_glbs = [
                GlbBin(
                    texcoord_bin,
                    "VEC2",
                    GlConstants.FLOAT,
                    unique_vertex_id,
                    None,
                    self.glb_bin_collector,
                )
                for texcoord_bin in texcoord_bins.values()
            ]

            joints_glb = None
            weights_glb = None
            if is_skin_mesh:
                joints_glb = GlbBin(
                    joints_bin,
                    "VEC4",
                    GlConstants.UNSIGNED_SHORT,
                    unique_vertex_id,
                    None,
                    self.glb_bin_collector,
                )
                weights_glb = GlbBin(
                    weights_bin,
                    "VEC4",
                    GlConstants.FLOAT,
                    unique_vertex_id,
                    None,
                    self.glb_bin_collector,
                )

            morph_pos_glbs = None
            morph_normal_glbs = None
            if len(shape_pos_bin_dic.keys()) != 0:
                morph_pos_glbs = [
                    GlbBin(
                        morph_pos_bin,
                        "VEC3",
                        GlConstants.FLOAT,
                        unique_vertex_id,
                        morph_minmax,
                        self.glb_bin_collector,
                    )
                    for morph_pos_bin, morph_minmax in zip(
                        shape_pos_bin_dic.values(), shape_min_max_dic.values()
                    )
                ]
                if vrm_version.startswith("0."):
                    morph_normal_glbs = [
                        GlbBin(
                            morph_normal_bin,
                            "VEC3",
                            GlConstants.FLOAT,
                            unique_vertex_id,
                            None,
                            self.glb_bin_collector,
                        )
                        for morph_normal_bin in shape_normal_bin_dic.values()
                    ]

            primitive_list = []
            for primitive_id, index_glb in primitive_glbs_dic.items():
                primitive: Dict[str, Any] = OrderedDict({"mode": 4})
                primitive["material"] = primitive_id
                primitive["indices"] = index_glb.accessor_id
                primitive["attributes"] = {
                    "POSITION": pos_glb.accessor_id,
                    "NORMAL": nor_glb.accessor_id,
                }
                if is_skin_mesh:
                    primitive["attributes"].update(
                        {
                            "JOINTS_0": joints_glb.accessor_id,
                            "WEIGHTS_0": weights_glb.accessor_id,
                        }
                    )
                primitive["attributes"].update(
                    {
                        "TEXCOORD_{}".format(i): uv_glb.accessor_id
                        for i, uv_glb in enumerate(uv_glbs)
                    }
                )
                if len(shape_pos_bin_dic.keys()) != 0:
                    vrm_version = self.vrm_version
                    if vrm_version is None:
                        raise Exception("vrm version is None")
                    if vrm_version.startswith("0."):
                        primitive["targets"] = [
                            {
                                "POSITION": morph_pos_glb.accessor_id,
                                "NORMAL": morph_normal_glb.accessor_id,
                            }
                            for morph_pos_glb, morph_normal_glb in zip(
                                morph_pos_glbs, morph_normal_glbs
                            )
                        ]
                    else:
                        primitive["targets"] = [
                            {"POSITION": morph_pos_glb.accessor_id}
                            for morph_pos_glb in morph_pos_glbs
                        ]
                    primitive["extras"] = {
                        "targetNames": list(shape_pos_bin_dic.keys())
                    }
                primitive_list.append(primitive)
            self.json_dic["meshes"].append(
                OrderedDict({"name": mesh.name, "primitives": primitive_list})
            )
            bm.free()
            # endregion hell

            bpy.ops.object.mode_set(mode="OBJECT")
        bpy.ops.object.mode_set(mode="OBJECT")

    def exporter_name(self) -> str:
        v = version()
        if os.environ.get("BLENDER_VRM_USE_TEST_EXPORTER_VERSION") == "true":
            v = (999, 999, 999)
        return "saturday06_blender_vrm_exporter_experimental_" + ".".join(map(str, v))

    def gltf_meta_to_dic(self) -> None:
        gltf_meta_dic = {
            "extensionsUsed": ["VRM", "KHR_materials_unlit", "VRMC_materials_mtoon"],
            "asset": {
                "generator": self.exporter_name(),
                "version": "2.0",  # glTF version
            },
        }

        self.json_dic.update(gltf_meta_dic)

    def vrm_meta_to_dic(self) -> None:
        # materialProperties は material_to_dic()で処理する
        # region vrm_extension
        vrm_extension_dic: Dict[str, Any] = OrderedDict()
        vrm_version = self.vrm_version
        if vrm_version is None:
            raise Exception("vrm version is None")
        if vrm_version.startswith("0."):
            vrm_extension_dic["exporterVersion"] = self.exporter_name()
        vrm_extension_dic["specVersion"] = self.vrm_version
        # region meta
        vrm_extension_dic["meta"] = vrm_meta_dic = {}
        # 安全側に寄せておく
        if vrm_version.startswith("0."):
            required_vrm_metas = vrm_types.Vrm0.REQUIRED_METAS
            vrm_metas = vrm_types.Vrm0.METAS
        else:
            required_vrm_metas = vrm_types.Vrm1.REQUIRED_METAS
            vrm_metas = vrm_types.Vrm1.METAS

        for k, v in required_vrm_metas.items():
            vrm_meta_dic[k] = self.armature[k] if k in self.armature.keys() else v
        for key in vrm_metas:
            vrm_meta_dic[key] = (
                self.armature[key] if key in self.armature.keys() else ""
            )

        if "texture" in self.armature.keys():
            thumbnail_index_list = [
                i
                for i, img in enumerate(self.glb_bin_collector.image_bins)
                if img.name == self.armature["texture"]
            ]
            if len(thumbnail_index_list) > 0:
                self.json_dic["samplers"].append(
                    {
                        "magFilter": 9729,
                        "minFilter": 9729,
                        "wrapS": 10497,
                        "wrapT": 10497,
                    }
                )
                self.json_dic["textures"].append(
                    {
                        "sampler": len(self.json_dic["samplers"]) - 1,
                        "source": thumbnail_index_list[0],
                    },
                )
                vrm_meta_dic["texture"] = len(self.json_dic["textures"]) - 1
        # endregion meta
        # region humanoid
        if vrm_version.startswith("0."):
            vrm_extension_dic["humanoid"] = {"humanBones": []}
            vrm0_humanoid_dic = vrm_extension_dic["humanoid"]
            node_name_id_dic = {
                node["name"]: i for i, node in enumerate(self.json_dic["nodes"])
            }
            for humanbone in (
                vrm_types.HumanBones.requires + vrm_types.HumanBones.defines
            ):
                if (
                    humanbone in self.armature.data.keys()
                    and self.armature.data[humanbone]
                    and self.armature.data[humanbone] in node_name_id_dic
                ):
                    vrm0_humanoid_dic["humanBones"].append(
                        {
                            "bone": humanbone,
                            "node": node_name_id_dic[self.armature.data[humanbone]],
                            # TODO min,max,center,axisLength : useDef(ry):Trueなら不要な気がするのでほっとく
                            "useDefaultValues": True,
                        }
                    )
            vrm0_humanoid_dic.update(
                json.loads(
                    self.textblock2str(
                        bpy.data.texts[self.armature["humanoid_params"]]
                    ),
                    object_pairs_hook=OrderedDict,
                )
            )
        else:
            vrm_extension_dic["humanoid"] = {"humanBones": {}}
            vrm_humanoid_dic: Dict[str, Any] = vrm_extension_dic["humanoid"]
            node_name_id_dic = {
                node["name"]: i for i, node in enumerate(self.json_dic["nodes"])
            }
            for humanbone in (
                vrm_types.HumanBones.requires + vrm_types.HumanBones.defines
            ):
                if (
                    humanbone in self.armature.data.keys()
                    and self.armature.data[humanbone]
                    and self.armature.data[humanbone] in node_name_id_dic
                ):
                    vrm_humanoid_dic["humanBones"].update(
                        {
                            humanbone: {
                                "node": node_name_id_dic[self.armature.data[humanbone]]
                            }
                        }
                    )

        # endregion humanoid
        # region firstPerson
        vrm_extension_dic["firstPerson"] = vrm_fp_dic = {}
        vrm_fp_dic.update(
            json.loads(
                self.textblock2str(bpy.data.texts[self.armature["firstPerson_params"]]),
                object_pairs_hook=OrderedDict,
            )
        )
        if (
            "firstPersonBone" in vrm_fp_dic.keys()
            and vrm_fp_dic["firstPersonBone"] != -1
        ):
            vrm_fp_dic["firstPersonBone"] = node_name_id_dic[
                vrm_fp_dic["firstPersonBone"]
            ]
        if "meshAnnotations" in vrm_fp_dic.keys():
            for mesh_annotation in vrm_fp_dic["meshAnnotations"]:
                mesh_annotation["mesh"] = [
                    i
                    for i, mesh in enumerate(self.json_dic["meshes"])
                    if mesh["name"] == mesh_annotation["mesh"]
                ][0]
                # TODO VRM1.0 is using node index that has mesh
        # TODO
        if vrm_version.startswith("1."):
            vrm_extension_dic["lookAt"] = vrm_look_at_dic = {}
            vrm_look_at_dic.update(
                json.loads(
                    self.textblock2str(bpy.data.texts[self.armature["lookat_params"]]),
                    object_pairs_hook=OrderedDict,
                )
            )

        # endregion firstPerson
        # region blendShapeMaster
        blendshape_group_name = (
            "blendShapeMaster" if vrm_version.startswith("0.") else "blendShape"
        )
        vrm_extension_dic[blendshape_group_name] = vrm_blend_shape_groups_dic = {}
        blend_shape_groups = json.loads(
            self.textblock2str(bpy.data.texts[self.armature["blendshape_group"]]),
            object_pairs_hook=OrderedDict,
        )

        # meshを名前からid
        # weightを0-1から0-100に
        # shape_indexを名前からindexに
        def clamp(min_val: float, val: float, max_val: float) -> float:
            if max_val >= val:
                if val >= min_val:
                    return val
                print(
                    "blendshapeGroup weight is between 0 and 1, value is {}".format(val)
                )
                return min_val
            print("blendshapeGroup weight is between 0 and 1, value is {}".format(val))
            return max_val

        for blend_shape_group in blend_shape_groups:
            for bind in blend_shape_group["binds"]:
                # TODO VRM1.0 is using node index that has mesh
                bind["mesh"] = [
                    i
                    for i, mesh in enumerate(self.json_dic["meshes"])
                    if mesh["name"] == bind["mesh"]
                ][0]
                bind["index"] = self.json_dic["meshes"][bind["mesh"]]["primitives"][0][
                    "extras"
                ]["targetNames"].index(bind["index"])
                bind["weight"] = (
                    clamp(0, bind["weight"] * 100, 100)
                    if vrm_version.startswith("0.")
                    else clamp(0, bind["weight"], 1)
                )
            if vrm_version.startswith("1."):
                for matval in blend_shape_group["materialValues"]:
                    matval["material"] = [
                        i
                        for i, mat in enumerate(self.json_dic["materials"])
                        if mat["name"] == matval["material"]
                    ][0]
        # TODO isBinary handle : 0 or 1 にするフラグ
        vrm_blend_shape_groups_dic["blendShapeGroups"] = blend_shape_groups
        # endregion blendShapeMaster

        # region secondaryAnimation
        springbone_name = (
            "springBone" if vrm_version.startswith("1.") else "secondaryAnimation"
        )
        vrm_extension_dic[springbone_name] = {"boneGroups": [], "colliderGroups": []}

        # region colliderGroups
        # armatureの子emptyを変換する
        collider_group_list = []
        empty_dic: Dict[int, Any] = {
            node_name_id_dic[ch.parent_bone]: []
            for ch in self.armature.children
            if ch.type == "EMPTY"
        }
        for child_empty in [ch for ch in self.armature.children if ch.type == "EMPTY"]:
            empty_dic[node_name_id_dic[child_empty.parent_bone]].append(child_empty)
        for node_id, empty_objs in empty_dic.items():
            colliders: List[Any] = []
            collider_group = {"node": node_id, "colliders": colliders}
            for empty in empty_objs:
                collider = {}
                empty_offset_pos = [
                    empty.matrix_world.to_translation()[i]
                    - self.armature.location[i]
                    - self.armature.data.bones[empty.parent_bone].head_local[i]
                    for i in range(3)
                ]
                if vrm_version.startswith("0."):
                    collider["radius"] = empty.empty_display_size
                    collider["offset"] = OrderedDict(
                        zip(
                            ("x", "y", "z"),
                            self.axis_blender_to_glb(empty_offset_pos),
                        )
                    )
                    collider["offset"]["z"] = collider["offset"]["z"] * -1
                else:
                    collider["size"] = [empty.empty_display_size]
                    collider["offset"] = self.axis_blender_to_glb(empty_offset_pos)
                    collider["shapeType"] = "sphere"
                colliders.append(collider)
            collider_group_list.append(collider_group)

        vrm_extension_dic[springbone_name]["colliderGroups"] = collider_group_list
        # endregion colliderGroups

        # region boneGroup
        # ボーン名からnode_idに
        # collider_groupも名前からcolliderGroupのindexに直す
        collider_node_id_list = [c_g["node"] for c_g in collider_group_list]
        bone_groups = json.loads(
            self.textblock2str(bpy.data.texts[self.armature["spring_bone"]]),
            object_pairs_hook=OrderedDict,
        )
        for bone_group in bone_groups:
            bone_group["bones"] = [
                node_name_id_dic[name] for name in bone_group["bones"]
            ]
            bone_group["colliderGroups"] = [
                collider_node_id_list.index(node_name_id_dic[name])
                for name in bone_group["colliderGroups"]
            ]
        vrm_extension_dic[springbone_name]["boneGroups"] = bone_groups
        # endregion boneGroup
        # endregion secondaryAnimation
        extension_name = "VRM" if vrm_version.startswith("0.") else "VRMC_vrm"
        self.json_dic["extensions"][extension_name].update(vrm_extension_dic)
        # endregion vrm_extension

        # region secondary
        self.json_dic["nodes"].append(
            {
                "name": "secondary",
                "translation": [0.0, 0.0, 0.0],
                "rotation": [0.0, 0.0, 0.0, 1.0],
                "scale": [1.0, 1.0, 1.0],
            }
        )
        self.json_dic["scenes"][0]["nodes"].append(len(self.json_dic["nodes"]) - 1)

    def finalize(self) -> None:
        bin_json, self.bin = self.glb_bin_collector.pack_all()
        self.json_dic.update(bin_json)
        magic = b"glTF" + struct.pack("<I", 2)
        json_str = json.dumps(self.json_dic).encode("utf-8")
        if len(json_str) % 4 != 0:
            json_str += b"\x20" * (4 - len(json_str) % 4)
        json_size = struct.pack("<I", len(json_str))
        if len(self.bin) % 4 != 0:
            for _ in range(4 - len(self.bin) % 4):
                self.bin += b"\x00"
        bin_size = struct.pack("<I", len(self.bin))
        total_size = struct.pack(
            "<I", len(json_str) + len(self.bin) + 28
        )  # include header size
        self.result = (
            magic
            + total_size
            + json_size
            + b"JSON"
            + json_str
            + bin_size
            + b"BIN\x00"
            + self.bin
        )
