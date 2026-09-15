import re

from src.services.utils.helper import Helper


def split_prompt_for_anthropic(raw_prompt: str, variables: dict, configuration: dict, service: str) -> dict:
        """
        Split prompt into blocks for Anthropic service based on variable positions to handle length differences.
        Sets configuration["prompt_blocks"] with the split and substituted blocks.
        Returns missing variables dictionary.
        Returns early if service is not anthropic.
        """
        first_var_start = raw_prompt.find("{{")
        if first_var_start <= 0:
            return {}

        # Find all variable positions in raw_prompt
        var_starts = []
        var_ends = []
        pos = 0
        while True:
            start = raw_prompt.find("{{", pos)
            if start == -1:
                break
            end = raw_prompt.find("}}", start)
            if end == -1:
                break
            var_starts.append(start)
            var_ends.append(end)
            pos = end + 2

        missing_vars = {}

        if len(var_ends) >= 2:
            # Use second last variable end to isolate last variable (e.g., current_date)
            second_last_var_end = var_ends[-2]
            last_var_start = var_starts[-1]
            last_var_end = var_ends[-1]
            raw_block1 = raw_prompt[:first_var_start]  # Static prefix
            raw_block2 = raw_prompt[first_var_start:second_last_var_end + 2]  # Variables except last
            raw_block3 = raw_prompt[second_last_var_end + 2:last_var_start]  # Static text between variables
            raw_block4 = raw_prompt[last_var_start:]  # Last variable and any text after it

            # Substitute each block separately to handle length differences
            block1, mv1 = Helper.replace_variables_in_prompt(raw_block1, variables)
            block2, mv2 = Helper.replace_variables_in_prompt(raw_block2, variables)
            block3, mv3 = Helper.replace_variables_in_prompt(raw_block3, variables)
            block4, mv4 = Helper.replace_variables_in_prompt(raw_block4, variables)

            # Combine missing variables from all blocks
            missing_vars = {**mv1, **mv2, **mv3, **mv4}

            if block1:
                # If block2 is empty (no middle variables), use 3-block split
                if not block2 or not block2.strip():
                    if block3 and block3.strip():
                        configuration["prompt_blocks"] = [block1, block3, block4]
                    else:
                        configuration["prompt_blocks"] = [block1, block4]
                else:
                    # 4-block split when block2 has content
                    if block3 and block3.strip():
                        configuration["prompt_blocks"] = [block1, block2, block3, block4]
                    else:
                        configuration["prompt_blocks"] = [block1, block2, block4]
        else:
            # Only one variable - put last variable in block 4, static in block 1
            raw_block1 = raw_prompt[:first_var_start]  # Static prefix
            raw_block4 = raw_prompt[first_var_start:]  # Last variable and any text after it

            # Substitute each block separately
            block1, mv1 = Helper.replace_variables_in_prompt(raw_block1, variables)
            block4, mv4 = Helper.replace_variables_in_prompt(raw_block4, variables)

            # Combine missing variables from all blocks
            missing_vars = {**mv1, **mv4}

            if block1:
                # For single variable case, always use 2-block split (static + variable)
                configuration["prompt_blocks"] = [block1, block4]

        return missing_vars
